"""资源运行确认对话框：GitHub 行按需显示。"""

from __future__ import annotations

import unittest

try:
    from PySide6.QtWidgets import QApplication

    from app.widget.resource_run_confirm_dialog import ResourceRunConfirmDialog
except ImportError:  # pragma: no cover
    ResourceRunConfirmDialog = None
    QApplication = None


@unittest.skipIf(ResourceRunConfirmDialog is None, "PySide6 is not installed")
class ResourceRunConfirmDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, **kwargs: object) -> ResourceRunConfirmDialog:
        from PySide6.QtWidgets import QWidget

        self._parents = getattr(self, "_parents", [])
        parent = QWidget()
        self._parents.append(parent)
        params = {
            "parent": parent,
            "resource_name": "MeiYue",
            "contact": "some contact",
            "github": "",
        }
        params.update(kwargs)
        return ResourceRunConfirmDialog(**params)

    def _has_github_label(self, dialog: ResourceRunConfirmDialog) -> bool:
        from qfluentwidgets import CaptionLabel

        for label in dialog.findChildren(CaptionLabel):
            if label.text() == "GitHub":
                return True
        return False

    def test_github_row_hidden_when_not_provided(self) -> None:
        dialog = self._dialog()
        self.assertFalse(self._has_github_label(dialog))

    def test_github_row_shown_when_url_provided(self) -> None:
        dialog = self._dialog(github="https://github.com/owner/repo")
        self.assertTrue(self._has_github_label(dialog))

    def test_github_row_shown_when_owner_repo_provided(self) -> None:
        dialog = self._dialog(github_owner="owner", github_repo="repo")
        self.assertTrue(self._has_github_label(dialog))


if __name__ == "__main__":
    unittest.main()
