"""
core/msg_builder.py
解析消息模板构建具体发送的消息内容
"""

from utils.config import get_config
from utils.hitokoto import request_hitokoto
from utils.logger import setup_logger
from datetime import date

logger = setup_logger("msg_builder")


def build_message_with_openai() -> str:
    """
    通过 OpenAI 接口生成续火花消息，内容丰富，不超过20字
    """
    from openai import OpenAI

    import os

    config = get_config()
    openai_config = config.get("openai", {})
    api_key = os.getenv("OPENAI_API_KEY", openai_config.get("api_key", ""))
    model = openai_config.get("model", "MiniMax-M2.7")

    if not api_key:
        return get_config().get("messageTemplate", "续火花")

    client = OpenAI(api_key=api_key)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "你是一个擅长写续火花消息的助手。用户需要你生成一段不超过20字的续火花消息，内容要温馨、有趣、适合发给聊天对象。请直接输出消息内容，不要加引号或其他修饰。",
            },
            {"role": "user", "content": "生成一段续火花消息，直接输出内容不要思考过程"},
        ],
        extra_body={"reasoning_split": True},
    )

    print(response)

    return response.choices[0].message.content.strip()


def render_message(template: str) -> str:
    """把一段模板渲染成真正要发出去的内容。

    换行归一，两种 CRLF 都收成真 \\n：
      ① 字面 \\r\\n（反斜杠+r+反斜杠+n 四字符）→ 字面 \\n：手写 .env 可能是这样，
         不收的话下游会漏下一个裸露的 \\r 打进草稿。
      ② 真 CRLF（U+000D U+000A）→ 真 \\n：第三方内容（一言）可能带进来。
    两条规则作用在互不相交的字符集上（字面串里没有真的 CR/LF），顺序无所谓。
    只做这两步：不再额外 replace("\\r","\\n")，否则会把「字面 \\r + 真换行」
    这类组合也吞掉，而那是作者的本意。

    剩下的换行统一交给 `core/douyin_im.line_list`：那边把**字面 \\n 与真换行**
    都收成真换行再按行键入，所以这里给哪种形态都能发对。

    `[API]` 占位符换成一条一言（每次调用都重新取）。
    """
    message = str(template or "")
    message = message.replace("\\r\\n", "\\n").replace("\r\n", "\n")
    if "[API]" in message:
        api_content = request_hitokoto()
        if api_content:
            message = message.replace("[API]", api_content)
        else:
            # 一言没取到（网络不通 / 接口挂了 / 返回空正文）。以前这里会把
            # "[error] 无法获取一言内容" 当正文发出去，对方真的收到这行字。
            # 现在的口径：**整行**去掉 —— 只清占位符会留下一行空白，而模板里
            # 只有 [API] 时剩下的就是一条空消息。去掉之后没内容可发就返回空串，
            # 由 `core/douyin_im.type_and_send` 的空文本守卫拒绝发送，调用方记
            # 一条 `input_failed` —— 宁可这一轮不发，也不发空白或占位符。
            kept = [line for line in message.split("\n") if line.strip() != "[API]"]
            message = "\n".join(kept)
            if "[API]" in message:
                # 占位符夹在别的文字中间（"打卡[API]哦"）：清掉它，保住这句话
                message = message.replace("[API]", "")
            logger.warning("[消息] 一言没取到，已去掉模板里的占位符行")

    return message.strip()


def build_message(template: str = "") -> str:
    """取这个账号这一轮要发的内容。

    `template` 是账号专属内容（`TASKS[].message`）：**非空就用它**，为空
    （空串 / None / 只有空白）就回落到全局 `MESSAGE_TEMPLATE`。

    为什么口径是「空则回落」而不是「空则发空」：老配置的 TASKS 里根本没有
    `message` 这个键，它的语义必须是「这个账号没单独配过」，而不是「这个账号
    发一条空白」—— 后者会让所有存量账号一条都发不出去。真发空消息本来也做不到。
    """
    text = str(template or "").strip()
    if not text:
        text = get_config().get("messageTemplate", "续火花")
    return render_message(text)
