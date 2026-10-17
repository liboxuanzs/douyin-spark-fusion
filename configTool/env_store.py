"""读写项目根的 .env（位置见 paths.ENV_FILE）。

写入策略是「就地更新 + 只写真正变化的键」：
  * 只改动本工具管理的那些键，你手写的注释、空行和其它变量原样保留
    （用 python-dotenv 的 set_key / unset_key 实现，它们只重写自己那一行）；
  * **值没变的键一个字节都不动** —— 否则用户点一次「保存」，文件 mtime、备份和
    预览都会动，看起来像改了什么，其实什么都没改；
  * 整批改动先落到同目录临时文件、在临时文件上改完，再**一次性** `os.replace`
    到真身：中途失败（磁盘满、进程被杀）时原文件分毫不动，不会留下"写了一半"；
  * 写之前先备份成 `<名字>.bak-<时间戳>`（只保留最新几份，见 BACKUP_KEEP）。
"""

from __future__ import annotations

import inspect
import os
import re
import shutil
import time
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key

from configTool.models import Config
from configTool.paths import ENV_FILE

#: `.env` 写盘前的备份保留份数（见 `_backup`）：够翻账，又不会越攒越多。
BACKUP_KEEP = 5

#: `follow_symlinks` 是 python-dotenv 后加的形参（requirements.txt 钉的是 1.2.2，
#: 但部署机上那份可能是旧的），所以按**真实签名**判断，绝不写死。
_FOLLOW_SYMLINKS = "follow_symlinks"

HEADER = """# DouYinSparkFlow 配置文件
#
# 这个文件由本地配置生成器（configTool）自动读写：
#   - 下面那些由工具管理的变量会被就地更新
#   - 你手写的注释和其他变量不会被改动
#
# 要点：
# - TASKS 和 COOKIES_<抖音号> 是必填项，且必须保持单行
# - COOKIES_<抖音号> 的后缀必须与 TASKS 里该账号的 unique_id 完全一致
# - TASKS 里每项的 fingerprint 是该账号固定的浏览器指纹种子（由 configTool 分配，
#   与 profiles.json 里的那份一致），别手改 —— 改了等于让这个账号换设备
# - MESSAGE_TEMPLATE 用 \\n 表示换行
# - 本文件生成在项目根目录，主程序直接用；Docker → 复制 / 挂载为 ./config/.env
# - 工具自己的数据（profiles/、profiles.json、local.json）在 configTool/ 下
"""


def resolve_path(path=None) -> Path:
    return Path(path) if path else ENV_FILE


def _is_symlink(target) -> bool:
    """目标是不是软链（决定 set_key 会不会把软链换成普通文件）。"""
    try:
        return Path(target).is_symlink()
    except OSError:  # pragma: no cover - lstat 失败（权限/竞态）
        return False


def _dotenv_supports_follow_symlinks() -> bool:
    """本机这份 python-dotenv 的 `set_key` / `unset_key` 认不认 `follow_symlinks`？"""
    for func in (set_key, unset_key):
        try:
            params = inspect.signature(func).parameters
        except (TypeError, ValueError):  # pragma: no cover - 读不到签名（C 实现等）
            return False
        if _FOLLOW_SYMLINKS not in params:
            return False
    return True


def _symlink_write_problem(target) -> str:
    """`.env` 是软链、而本机 dotenv 改不动软链时返回中文原因（否则空串）。

    为什么专门拦这一下：python-dotenv 默认 `follow_symlinks=False`，它最后一步是
    `os.replace(tmp, <.env>)` —— 对软链来说那一步会把**软链本身换成普通文件**，
    真正被读取的那份配置压根没改（用户看到的却是「保存成功」）。
    本机 dotenv 支持 `follow_symlinks` 时我们跟着真身写；不支持就只能明确拒绝 ——
    绝不静默换掉用户的软链。

    口径与 `webui/envfile.py::symlink_write_problem` 一致（configTool 不能 import
    webui，所以这里是一份本地实现）。
    """
    target = Path(target)
    if not _is_symlink(target):
        return ""
    if _dotenv_supports_follow_symlinks():
        return ""
    try:
        real = os.path.realpath(str(target))
    except OSError:  # pragma: no cover
        real = "?"
    return (
        f"{target} 是一个软链接（指向 {real}），而本机安装的 python-dotenv 不认识 "
        f"follow_symlinks 参数：按默认写法保存会把软链接替换成普通文件，真正被读取的"
        f"那份配置其实没改（「保存了却不生效」）。请升级 python-dotenv"
        f"（requirements.txt 钉的是 1.2.2），或者直接把配置写进 {real}。"
    )


def _dotenv_write_kwargs(target) -> dict:
    """软链要跟着真身写时传给 dotenv 的额外参数（不支持时由调用方先拒绝）。"""
    return {_FOLLOW_SYMLINKS: True} if _is_symlink(target) else {}


def _write_target(target: Path) -> Path:
    """真正要被改的那个文件：`.env` 是软链时是它的真身。

    本机 dotenv 支持 `follow_symlinks` 时直接改真身（软链保持不动，指过去的还是
    那份配置）；不支持时调用方已经用 `_symlink_write_problem()` 拒绝过了，
    这里返回原路径只是兜底。
    """
    if _is_symlink(target) and _dotenv_supports_follow_symlinks():
        try:
            return Path(os.path.realpath(str(target)))
        except OSError:  # pragma: no cover
            return target
    return target


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def load_config(path=None) -> tuple:
    """返回 (配置, 提示列表)。文件不存在时返回默认配置。

    注意这里**不会**补占位账号：账号信息全部来自浏览器登录，凭空造一个
    「账号1」既没有抖音号也没有 Cookie，只会让界面显示一条假数据、还让校验
    一路报错。没有账号就返回空列表，界面会显示添加引导。
    """
    target = resolve_path(path)
    notes: list = []

    if not target.exists():
        notes.append(f"未找到 {target.name}，已载入默认值（首次保存时自动创建）")
        return Config(), notes

    try:
        # interpolate=False：默认值会把 `${昵称}` 这类写法按当前环境插值 ——
        # 读进来就已经不是文件里的原文了（面板显示、保存比对、回写全跟着错）。
        raw = dotenv_values(str(target), interpolate=False)
    except Exception as exc:
        notes.append(f"读取 {target.name} 失败：{type(exc).__name__}: {exc}")
        return Config(), notes

    mapping = {key: value for key, value in raw.items() if value is not None}
    config = Config.from_env_map(mapping)

    if not config.accounts:
        notes.append("TASKS 里没有账号 —— 点「＋ 添加账号」会自动打开浏览器登录")
    else:
        notes.append(f"已从 {target.name} 载入 {len(config.accounts)} 个账户")
    return config, notes


def _all_keys(target) -> list:
    """`.env` 里出现过的**所有**键名（含值为空的），用于比对「删掉了哪些键」。"""
    try:
        values = dotenv_values(str(target), interpolate=False)
    except Exception:
        return []
    return [key for key, value in values.items() if value is not None]


def read_keys(path=None) -> list:
    """只取**有值**的键名，用于检测残留的 COOKIES_* 与「已配置」判定。

    这里是真值过滤（曾经是 `value is not None`）：`COOKIES_x=` 这种空值不算
    「已配置」—— 主程序每轮都会跳过没有 Cookie 的账号，面板要是把它显示成
    「已配置」，用户会在"看起来配好了、其实一条都不发"的状态里干等。
    """
    target = resolve_path(path)
    if not target.exists():
        return []
    try:
        values = dotenv_values(str(target), interpolate=False)
    except Exception:
        return []
    return [key for key, value in values.items() if value]


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
class RiskyValueError(ValueError):
    """值会在 `.env` 里被改写/截断时拒绝保存（详见 `risky_values` 的说明）。"""


def _lock_down(target) -> None:
    """POSIX 上把权限收回 600 —— 这个文件里有 Cookie，等于账号本身。

    失败只放过：Windows 上 `os.chmod` 基本是空操作，为它让保存失败不值当。
    """
    try:
        os.chmod(target, 0o600)
    except (OSError, NotImplementedError, AttributeError):  # pragma: no cover - Windows
        pass


def _backup(target) -> str:
    """把当前 `.env` 复制成 `<名字>.bak-<时间戳>`，返回新文件名（没做成返回空串）。

    为什么非要留一份：这个文件是用户手工维护的（注释、别人的变量都在里面），
    「就地更新」的承诺一旦因为一个 bug 变成「整份重写」，备份就是唯一的退路。
    时间戳精确到秒，同一秒内连写两次也不覆盖（加 -2 后缀）；只保留最新
    `BACKUP_KEEP` 份，旧的删掉，免得越攒越多。
    """
    target = Path(target)
    if not target.exists():
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = target.with_name(f"{target.name}.bak-{stamp}")
    index = 2
    while dest.exists():  # 同一秒内连写两次也不覆盖
        dest = target.with_name(f"{target.name}.bak-{stamp}-{index}")
        index += 1
    try:
        shutil.copy2(target, dest)
        # copy2 会把**源文件**的 mtime 一起复制过来，而 _prune_backups 按 mtime
        # 排序只留最新几份 —— 刚生成的这份会被当成最旧、立刻被自己删掉。
        # 落盘后把 mtime 拨到现在。
        os.utime(dest, None)
    except OSError:
        return ""
    _prune_backups(target)
    return dest.name


def _prune_backups(target) -> None:
    """按 mtime 只留最新 BACKUP_KEEP 份备份（读不到/删不掉的都放过）。"""
    target = Path(target)
    try:
        olds = sorted(
            target.parent.glob(f"{target.name}.bak-*"),
            key=lambda item: (item.stat().st_mtime, item.name),
            reverse=True,
        )
    except OSError:
        return
    for stale in olds[BACKUP_KEEP:]:
        try:
            stale.unlink()
        except OSError:
            pass


def _atomic_apply(target: Path, changes: dict, *, seed_header: bool) -> None:
    """把整批键改完，再**一次性** `os.replace` 到真身。

    以前是"每个键一次 set_key"：每个键都会「读全文件 → 改一处 → 整份写回 +
    os.replace」（20 多个受管键就是 20 多次），中途失败/被 Ctrl-C 就留下一份写了一半
    的 .env。现在：

      1. 把当前文件复制成同目录临时文件（首次创建时写 HEADER 打底）；
      2. 在临时文件上把所有变化的键改完（set_key 保留注释与键序）；
      3. 读回文本**校验**这些键确实写进去了，再一次性 os.replace 到真身。

    任何一步失败：真身分毫不动，临时文件清掉，异常照原样抛给调用方。
    """
    temp = target.with_name(f".{target.name}.configtool-{os.getpid()}-{time.time_ns()}.tmp")
    try:
        if seed_header:
            temp.write_text(HEADER, encoding="utf-8")
        else:
            shutil.copy2(target, temp)
        # 先把临时文件收紧到 600：dotenv 的 rewrite() 拿它当模板继承 mode，
        # 而中间态里是整份 .env（含全部 COOKIES_），不能裸奔。
        _lock_down(temp)
        kwargs = _dotenv_write_kwargs(target)
        for key, value in changes.items():
            # quote_mode="never"：与 .env.example / index.html 的写法保持一致，不加引号，
            # 否则 Docker 的 env_file 解析器不会还原被转义的 \" ，JSON 值会坏掉。
            set_key(str(temp), key, value, quote_mode="never", encoding="utf-8", **kwargs)
        _lock_down(temp)

        # 读回文本校验：不能假设"set_key 没报错就一定写进去了"（失败就整批放弃）。
        written = set()
        for line in temp.read_text(encoding="utf-8").splitlines():
            name = line.split("=", 1)[0].strip()
            if name.startswith("export "):
                name = name[len("export "):].strip()
            if name:
                written.add(name)
        missing = [key for key in changes if key not in written]
        if missing:
            raise RiskyValueError(
                "写入校验失败，已放弃这次保存（原文件未改动）：" + "、".join(missing)
            )

        os.replace(temp, target)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def save_config(config: Config, path=None) -> tuple:
    """就地写入。返回 (提示列表, 残留的 COOKIES_* 列表)。

    写入前先做三道防线（都是"以前没有、出事看不出来"的类型）：

    1. **拒绝会被 dotenv 改写的值**：`set_key(..., quote_mode="never")` 原样落盘，
       而 `.env` 的读法把「空白 + #」之后当注释、把 `${...}` 当变量插值、首尾裁
       空白、换行断行 —— 写进去的是 A、读出来的是 B，且**这一次保存看起来完全
       成功**（下次启动才发现模板少了一半）。所以宁可当场失败、把键名和原因报给
       用户（`configTool/main.py` 的保存回调已有 `except Exception` → 面板上显示
       "保存失败：…"），也不做隐式转义：TASKS / MESSAGE_TEMPLATE 是 JSON 与含 `\n`
       的文本，一旦改成引号包裹，Docker 的 `env_file` 解析器（不还原 `\"`）会把
       它们读坏，是更大的坑。

    2. **软链不能被换成普通文件**：`.env` 是软链时按默认写法保存等于把软链本身替换
       掉，真身没改（"保存了却不生效"）。本机 dotenv 支持 `follow_symlinks` 就跟着
       真身写，不支持就明确拒绝（`RiskyValueError`）。

    3. **写前备份 + 原子替换 + 只写变化的键**：见 `_backup` / `_atomic_apply`。
       缺一不可 —— 没有备份，一次 bug 就能把用户手写的 .env 抹掉；不原子，中途崩掉
       会留下写了一半的配置；不比对，每次保存都在抖动整个文件。

    另外**写完收紧权限到 600**：这个文件里是全部 COOKIES_<unique_id>
    （= 抖音 sessionid，等于账号本身）。以默认 umask 落盘就是 644，同机任何用户
    都能读走，打包/备份/快照也会一起带走。`deploy/install.sh` 与
    `docker/entrypoint-cron.sh` 都已经是 600，图形工具这条路径漏了。
    非 POSIX（Windows 上 `os.chmod` 基本是空操作）失败只告警，不影响保存。
    """
    target = resolve_path(path)
    notes: list = []

    risky = risky_values(config)
    if risky:
        detail = "；".join(f"{key}（{why}）" for key, why in risky)
        raise RiskyValueError(
            f"有 {len(risky)} 个值会被 .env 的解析规则改写，已拒绝保存：{detail}。"
            "请改掉这些字符后再存（值里含 # 时可在它前面加空格之外的写法，"
            "或把模板里的“#”换成全角“＃”；含 ${ 的写法会被当成变量插值，"
            "请换成不触发插值的写法）—— 直接写进去会静默截断/改写。"
        )

    # 软链：本机 dotenv 改不动它时宁可拒绝，也绝不把用户的软链换成普通文件。
    symlink_problem = _symlink_write_problem(target)
    if symlink_problem:
        raise RiskyValueError(symlink_problem)

    # 真正要改的文件（软链时是它的真身）；备份、比对、写盘都以它为准。
    write_path = _write_target(target)
    created = not write_path.exists()

    # 先算出"哪些键真的变了"：值没变的一个字节都不碰。
    managed = config.to_env_map()
    current: dict = {}
    if not created:
        try:
            current = {
                key: value
                for key, value in dotenv_values(str(write_path), interpolate=False).items()
                if value is not None
            }
        except Exception:  # pragma: no cover - 读失败就当"全变了"，写盘仍走原子替换
            current = {}
    changed = {key: value for key, value in managed.items() if current.get(key) != value}

    if not changed:
        # 没有变化就别写：重写一遍会刷新 mtime、滚动备份，还可能被别的进程读到半个文件。
        _lock_down(write_path)
        notes.append("没有需要写入的变化")
    else:
        if created:
            write_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            # 写之前先收紧**已存在**文件的权限，理由不是"好看"：python-dotenv 的
            # rewrite() 先 `lstat` 取原 mode，写完把临时文件 chmod 成同一个 mode 再
            # os.replace（`NamedTemporaryFile(prefix=".tmp_", dir=<.env 所在目录>)`）。
            # 也就是说 .env 还是 644 时，那个**装着全部 COOKIES_** 的中间态文件也是
            # 644 —— 进程在 replace 之前被杀掉，就留下一个 644 的完整配置。先收紧，
            # 中间态继承到的就是 600。放在拒绝检查之后：拒绝保存时连权限都不动。
            _lock_down(write_path)
            backup = _backup(write_path)
            if backup:
                notes.append(f"写入前已备份为 {backup}")
        _atomic_apply(write_path, changed, seed_header=created)
        if created:
            notes.append(f"已创建 {target.name}")

    # 收尾再收一次权限：首建时 set_key 会以默认 umask 创建文件，中间态可能还是 644。
    _lock_down(write_path)

    orphans = sorted(
        key
        for key in _all_keys(write_path)
        if key.startswith("COOKIES_") and key not in managed
    )
    if orphans:
        notes.append(f"检测到 {len(orphans)} 个未使用的旧 Cookie 变量：{', '.join(orphans)}")

    return notes, orphans


def unset_keys(keys, path=None) -> int:
    """删除指定键，返回**真实删除**的个数。

    以前用 `unset_key(...)` 的返回值计数，而它返回的是 `(是否改过, 键名)` 这样一个
    非空元组 —— 对**不存在**的键同样为真。于是"已清理 N 个"永远等于入参个数，
    界面上报的数字是假的（用户以为孤儿键都清掉了，其实一个都没删）。
    改成比较删除前后的键集合，报出真正删掉的个数。
    """
    target = resolve_path(path)
    if not target.exists():
        return 0
    # 软链：本机 dotenv 改不动它时明确拒绝，绝不静默把软链换成普通文件。
    symlink_problem = _symlink_write_problem(target)
    if symlink_problem:
        raise RiskyValueError(symlink_problem)

    write_path = _write_target(target)
    kwargs = _dotenv_write_kwargs(target)
    before = set(_all_keys(write_path))
    for key in keys:
        if key not in before:
            continue  # 不存在的键直接跳过，别让 dotenv 的返回值记账
        try:
            unset_key(str(write_path), key, quote_mode="never", encoding="utf-8", **kwargs)
        except Exception:
            continue
    return len(before - set(_all_keys(write_path)))


# ---------------------------------------------------------------------------
# 值层面的风险检查
# ---------------------------------------------------------------------------
def risky_values(config: Config) -> list:
    """返回 [(键, 说明)]：值会在 `.env` 里被**读坏**（写进去的是 A、读出来的是 B）。

    四类（都是 `set_key(..., quote_mode="never")` 原样落盘 + dotenv 的读法导致的）：
      * 「空白 + `#`」：`#` 前面有空白时，dotenv 把从这里到行尾的内容当**行内注释**
        截掉（`话题#打卡` 这种**没有空白**的 `#` 是合法的，不能误伤）；
      * `${`：会被当成**变量插值**，读取时按当前环境替换掉（`你好${昵称}今天`
        读出来是 `你好今天`）；而且面板/本工具一旦把这个"读出来的值"再写回去，
        原文就永久丢了 —— 这一条是不可逆的；
      * 首尾空白：dotenv 会把值两端的空白裁掉；
      * 裸换行：真换行会把一个键拆成两行，第二行变成一条无效的 `KEY=VALUE`
        （`to_env_map` 里 MESSAGE_TEMPLATE 的真换行已被换成字面 `\\n`，
        这里兜的是别的键 / 未来新增的键）。

    改写成"加引号"**不是**可选项：Docker 的 `env_file` 解析器不还原引号里的 `\\"`
    （见 `to_env_map` 里 TASKS / MESSAGE_TEMPLATE 用引号的代价），把值包起来会
    让 JSON 与模板在 Docker 部署下直接坏掉。所以调用方选择**拒绝保存**并把键名
    报出来，让用户自己决定怎么改。
    """
    risky: list = []
    for key, value in config.to_env_map().items():
        text = "" if value is None else str(value)
        if re.search(r"(?:^|\s)#", text):
            risky.append((key, "值里有空白开头的 # ，.env 解析时会被当成行内注释截断"))
        elif "${" in text:
            risky.append((key, "值里含 ${ ，.env 读取时会被当成变量插值改写（写进去是 A、读出来是 B）"))
        elif text != text.strip():
            risky.append((key, "值首尾有空白，.env 解析器可能裁掉"))
        elif "\n" in text or "\r" in text:
            risky.append((key, "值里有真换行，会把一个键拆成两行"))
    return risky
