"""手机发来的图片、文件：先攒着等下一句话；只发附件就到点直接让 Claude 看；富文本里的图直接处理。

跑法：python -m unittest discover -s tests -v
"""
import json
import sys
import time
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fb.transport import Incoming  # noqa: E402
from test_flow import ME, _Base, wait_for  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class AttachmentTest(_Base):
    def setUp(self):
        super().setUp()
        self.cfg.inbox_dir = Path(self.tmp.name) / "收件"
        self.cfg.attach_wait_seconds = 30
        self.tx.resources = {"img_1": PNG, "img_2": PNG, "file_1": b"a,b\n1,2\n"}

    def send_raw(self, msg_type: str, content: dict, parent: str = "") -> str:
        self._n += 1
        mid = f"um_raw_{self._n}_{time.time_ns()}"
        self.b.on_message(Incoming(msg_id=mid, open_id=ME, chat_id="oc_1", chat_type="p2p", msg_type=msg_type,
                                   content=json.dumps(content, ensure_ascii=False),
                                   create_time_ms=int(time.time() * 1000), parent_id=parent))
        return mid

    def saved(self):
        return sorted(self.cfg.inbox_dir.rglob("*.*"))

    def test_image_then_text(self):
        mid = self.send_raw("image", {"image_key": "img_1"})
        self.assertIn("收到图片", wait_for(lambda: self.replies_to(mid))[0]["text"])
        mid2 = self.send_raw("image", {"image_key": "img_2"})
        self.assertIn("攒了 2 个", wait_for(lambda: self.replies_to(mid2))[0]["text"])
        t = self.wait_status(self.task_of(self.send("这是什么报错"))["id"], "done")
        files = self.saved()
        self.assertEqual(len(files), 2)
        self.assertTrue(all(f.suffix == ".png" for f in files))
        for f in files:
            self.assertIn(str(f), t["prompt"])
        self.assertTrue(t["prompt"].startswith("这是什么报错"))
        a = self.argv()[-1]
        self.assertEqual(a[a.index("--add-dir") + 1], str(self.cfg.inbox_dir))
        # 附件只配给紧接着的那一句，下一句不再带
        t2 = self.wait_status(self.task_of(self.send("另一个问题"))["id"], "done")
        self.assertEqual(t2["prompt"], "另一个问题")

    def test_image_alone_runs_after_wait(self):
        self.cfg.attach_wait_seconds = 0.5
        mid = self.send_raw("image", {"image_key": "img_1"})
        t = self.wait_status(self.task_of(mid)["id"], "done")
        self.assertIn("看看这些附件", t["prompt"])
        self.assertIn(str(self.saved()[0]), t["prompt"])

    def test_file_keeps_name(self):
        self.send_raw("file", {"file_key": "file_1", "file_name": "10月报表.csv"})
        self.wait_status(self.task_of(self.send("汇总一下"))["id"], "done")
        self.assertTrue(self.saved()[0].name.endswith("10月报表.csv"))
        self.assertEqual(self.saved()[0].read_bytes(), b"a,b\n1,2\n")

    def test_post_with_image_runs_directly(self):
        post = {"title": "", "content": [[{"tag": "text", "text": "看下这张图"}],
                                          [{"tag": "img", "image_key": "img_1"}]]}
        mid = self.send_raw("post", post)
        t = self.wait_status(self.task_of(mid)["id"], "done")
        self.assertTrue(t["prompt"].startswith("看下这张图"))
        self.assertIn(str(self.saved()[0]), t["prompt"])

    def test_download_failure(self):
        mid = self.send_raw("image", {"image_key": "img_missing"})
        self.assertIn("没下载下来", wait_for(lambda: self.replies_to(mid))[0]["text"])
        self.assertEqual(self.store.recent(5), [])


if __name__ == "__main__":
    unittest.main()
