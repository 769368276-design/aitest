import os

from browser_use.tools.service import Tools
from browser_use.tools.views import ClickElementAction, ClickElementActionIndexOnly, UploadFileAction


class QATools(Tools):
    def __init__(self, runner, *args, **kwargs):
        self._runner = runner
        super().__init__(*args, **kwargs)

    def _register_click_action(self) -> None:
        if "click" in self.registry.registry.actions:
            del self.registry.registry.actions["click"]

        async def _maybe_upload_instead_of_click(
            index: int | None,
            browser_session,
            available_file_paths,
            file_system,
        ):
            if index is None:
                return None
            try:
                case_step_no = int(getattr(self._runner, "_case_step_last_seen", 0) or 0)
            except Exception:
                case_step_no = 0
            if case_step_no <= 0:
                try:
                    case_step_no = int(self._runner._get_next_pending_transfer_file_step_no() or 0)
                except Exception:
                    case_step_no = 0
            if case_step_no <= 0:
                return None
            try:
                if not self._runner._case_step_requires_upload_file(case_step_no):
                    return None
            except Exception:
                return None
            try:
                step = self._runner._find_case_step_by_number(case_step_no)
            except Exception:
                step = None
            try:
                path = self._runner._ensure_transfer_file_disk_path(case_step_no, step) if step else None
            except Exception:
                path = None
            path = str(path or "").strip()
            if not path or not os.path.exists(path):
                return None

            try:
                selector_map = await browser_session.get_selector_map()
            except Exception:
                selector_map = {}
            node = None
            try:
                node = selector_map.get(int(index)) if isinstance(selector_map, dict) else None
            except Exception:
                node = None
            if not node:
                return None

            try:
                any_file_input = bool(browser_session.is_file_input(node))
            except Exception:
                any_file_input = False
            if not any_file_input:
                try:
                    for el in (selector_map or {}).values():
                        try:
                            if browser_session.is_file_input(el):
                                any_file_input = True
                                break
                        except Exception:
                            continue
                except Exception:
                    any_file_input = False
            if not any_file_input:
                return None

            try:
                is_direct_file_input = bool(browser_session.is_file_input(node))
            except Exception:
                is_direct_file_input = False
            if not is_direct_file_input:
                blob = ""
                try:
                    blob = (node.get_all_children_text() or "").strip()
                except Exception:
                    blob = ""
                try:
                    attrs = getattr(node, "attributes", None) or {}
                    for k in ("aria-label", "title", "placeholder", "id", "name", "class"):
                        v = str(attrs.get(k) or "").strip()
                        if v:
                            blob = (blob + "\n" + v) if blob else v
                except Exception:
                    pass
                low = blob.lower()
                cn_keys = ["上传", "选择文件", "选择图片", "上传图片", "导入", "附件", "拖拽"]
                en_keys = ["upload", "choose file", "select file", "select image", "drag and drop", "import", "attachment"]
                looks_like_upload = any(k in blob for k in cn_keys) or any(k in low for k in en_keys)
                if not looks_like_upload:
                    return None

            action = self.registry.registry.actions.get("upload_file")
            if not action:
                return None
            params = UploadFileAction(index=int(index), path=path)
            return await action.function(
                params=params,
                browser_session=browser_session,
                available_file_paths=list(available_file_paths or []),
                file_system=file_system,
            )

        if self._coordinate_clicking_enabled:
            @self.registry.action(
                "Click element by index or coordinates. Use coordinates only if the index is not available. Either provide coordinates or index.",
                param_model=ClickElementAction,
            )
            async def click(
                params: ClickElementAction,
                browser_session,
                available_file_paths=None,
                file_system=None,
            ):
                if params.index is None and (params.coordinate_x is None or params.coordinate_y is None):
                    from browser_use.agent.views import ActionResult

                    return ActionResult(error="Must provide either index or both coordinate_x and coordinate_y")
                if params.index is not None:
                    replaced = await _maybe_upload_instead_of_click(params.index, browser_session, available_file_paths, file_system)
                    if replaced is not None:
                        return replaced
                    return await self._click_by_index(params, browser_session)
                return await self._click_by_coordinate(params, browser_session)
        else:
            @self.registry.action("Click element by index.", param_model=ClickElementActionIndexOnly)
            async def click(
                params: ClickElementActionIndexOnly,
                browser_session,
                available_file_paths=None,
                file_system=None,
            ):
                replaced = await _maybe_upload_instead_of_click(params.index, browser_session, available_file_paths, file_system)
                if replaced is not None:
                    return replaced
                return await self._click_by_index(params, browser_session)
