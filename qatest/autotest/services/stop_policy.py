from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StopPolicyConfig:
    submit_observation_wait_ms: int = 1500
    stop_check_min_step: int = 3
    non_blocking_note_max: int = 20
    escalate_non_blocking_on_step_done: bool = False


@dataclass(frozen=True)
class StopDecision:
    stop: bool
    stop_reason: str = ""
    blocking: bool = False
    create_bug: bool = False
    note: str = ""


class StopPolicy:
    def __init__(self, config: StopPolicyConfig | None = None):
        self.config = config or StopPolicyConfig()

    @classmethod
    def from_settings(cls, settings_obj) -> "StopPolicy":
        try:
            cfg = StopPolicyConfig(
                submit_observation_wait_ms=int(getattr(settings_obj, "AI_EXEC_SUBMIT_OBSERVE_WAIT_MS", 1500) or 1500),
                stop_check_min_step=int(getattr(settings_obj, "AI_EXEC_STOP_CHECK_MIN_STEP", 3) or 3),
                non_blocking_note_max=int(getattr(settings_obj, "AI_EXEC_NON_BLOCKING_NOTE_MAX", 20) or 20),
                escalate_non_blocking_on_step_done=bool(
                    getattr(settings_obj, "AI_EXEC_ESCALATE_NON_BLOCKING_ON_STEP_DONE", False)
                ),
            )
        except Exception:
            cfg = StopPolicyConfig()
        return cls(cfg)

    @classmethod
    def from_settings_and_overrides(cls, settings_obj, overrides: dict | None) -> "StopPolicy":
        base = cls.from_settings(settings_obj).config
        ov = overrides if isinstance(overrides, dict) else {}
        def _pick_int(key: str, default: int) -> int:
            try:
                v = ov.get(key, default)
                return int(v)
            except Exception:
                return int(default)
        def _pick_bool(key: str, default: bool) -> bool:
            try:
                v = ov.get(key, default)
                if isinstance(v, bool):
                    return v
                if isinstance(v, (int, float)):
                    return bool(v)
                s = str(v).strip().lower()
                if s in ("1", "true", "yes", "on"):
                    return True
                if s in ("0", "false", "no", "off"):
                    return False
                return bool(default)
            except Exception:
                return bool(default)
        cfg = StopPolicyConfig(
            submit_observation_wait_ms=_pick_int("submit_observation_wait_ms", base.submit_observation_wait_ms),
            stop_check_min_step=_pick_int("stop_check_min_step", base.stop_check_min_step),
            non_blocking_note_max=_pick_int("non_blocking_note_max", base.non_blocking_note_max),
            escalate_non_blocking_on_step_done=_pick_bool(
                "escalate_non_blocking_on_step_done",
                base.escalate_non_blocking_on_step_done,
            ),
        )
        return cls(cfg)

    def should_run_stop_check(self, step_number: int) -> bool:
        try:
            return int(step_number) >= int(self.config.stop_check_min_step)
        except Exception:
            return False

    def submit_wait_ms(self) -> int:
        try:
            return max(0, int(self.config.submit_observation_wait_ms))
        except Exception:
            return 1500

    def should_escalate_non_blocking_on_step_done(self, has_non_blocking_notes: bool) -> bool:
        return bool(has_non_blocking_notes) and bool(self.config.escalate_non_blocking_on_step_done)

    def decide_after_blocking_check(self, ok: bool) -> StopDecision:
        if ok:
            return StopDecision(stop=False)
        return StopDecision(stop=True, stop_reason="assert_failed", blocking=True, create_bug=True)

    def decide_after_non_blocking_escalation(self, should_escalate: bool) -> StopDecision:
        if not should_escalate:
            return StopDecision(stop=False)
        return StopDecision(stop=True, stop_reason="non_blocking_bug", blocking=True, create_bug=True)
