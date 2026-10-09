import logging
import unittest

from utils.logger import LOG_FILE, _file_handler, setup_logger


class LoggerTests(unittest.TestCase):
    def tearDown(self):
        for name in ('test-logger', 'test-logger-b'):
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                # 共用的那个文件 handler **不能关**：它是全进程共享的，
                # 关掉之后所有 logger 都写不进日志了（`_file_handler()` 下次
                # 会重建，但重建就意味着这段日志真的丢了）。
                if handler is not _file_handler():
                    handler.close()

    def test_setup_logger_updates_existing_handler_levels(self):
        logger = setup_logger(name='test-logger', level='Info')
        self.assertEqual(logger.level, logging.INFO)
        self.assertEqual(logger.handlers[0].level, logging.INFO)

        logger = setup_logger(name='test-logger', level='Debug')

        self.assertEqual(logger.level, logging.DEBUG)
        self.assertEqual(logger.handlers[0].level, logging.DEBUG)

    def test_all_loggers_share_one_file_handler(self):
        """回归：以前是每个 logger 名各建一个 RotatingFileHandler 写同一个 app.log，
        轮转时互相覆盖归档文件（详见 utils/logger.py 里 `_file_handler` 的注释）。
        """
        first = setup_logger(name='test-logger', level='Info')
        second = setup_logger(name='test-logger-b', level='Info')

        files = [
            handler
            for handler in first.handlers + second.handlers
            if isinstance(handler, logging.FileHandler)
        ]
        self.assertEqual(len(files), 2, "两个 logger 各自都该有文件 handler")
        self.assertIs(files[0], files[1], "但它们必须是同一个对象")

        # 共用的 handler 不跟着单个 logger 的档位走 —— 它是全局的，
        # 否则后建的 logger 会把先建的 logger 的写盘档位一起改掉。
        self.assertEqual(files[0].level, logging.NOTSET)
        self.assertFalse(
            hasattr(files[0], "shouldRollover"),
            "进程内不做轮转：轮转归 deploy/logrotate.conf（copytruncate）",
        )
        self.assertEqual(files[0].baseFilename, LOG_FILE)

    def test_closed_shared_handler_is_rebuilt(self):
        """handler 被别处 close 掉之后要能重建，否则后续日志全丢。"""
        original = _file_handler()
        original.close()
        rebuilt = _file_handler()
        self.assertIsNot(rebuilt, original)
        self.assertFalse(rebuilt.stream.closed)


if __name__ == '__main__':
    unittest.main()
