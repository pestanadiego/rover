from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rover.common import clean_text, non_negative_float, positive_int, read_yaml_mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = PROJECT_ROOT / "config" / "keyword_policy.yaml"

BUCKET_ORDER = (
    "manual",
    "active_proven",
    "winner_mutation",
    "new_generated",
    "cooldown_retry",
)

DEFAULT_SELECTION_MIX = {
    "manual": 0.10,
    "active_proven": 0.25,
    "winner_mutation": 0.30,
    "new_generated": 0.25,
    "cooldown_retry": 0.10,
}

DEFAULT_COOLDOWNS = {
    "short_days": 2,
    "medium_days": 7,
    "long_days": 30,
    "no_results_days": 2,
    "error_days": 7,
    "stale_days": 30,
}

DEFAULT_DIVERSITY = {
    "max_share_per_origin": 0.35,
    "max_share_per_cluster": 0.25,
    "max_per_parent_keyword": 2,
}


@dataclass(frozen=True)
class KeywordPolicy:
    target_keyword_count: int
    selection_mix: dict[str, float]
    cooldowns: dict[str, int]
    retire_after_zero_winner_runs: int
    diversity: dict[str, float | int]
    banned_terms: tuple[str, ...] = field(default_factory=tuple)
    default_cluster: str = "dummy"
    path: Path = DEFAULT_POLICY_PATH

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_keyword_count": self.target_keyword_count,
            "selection_mix": dict(self.selection_mix),
            "cooldowns": dict(self.cooldowns),
            "retire_after_zero_winner_runs": self.retire_after_zero_winner_runs,
            "diversity": dict(self.diversity),
            "banned_terms": list(self.banned_terms),
            "default_cluster": self.default_cluster,
            "path": str(self.path),
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


def load_keyword_policy(path: str | Path | None = None) -> KeywordPolicy:
    policy_path = resolve_policy_path(path)
    data = read_yaml_mapping(policy_path, description="Keyword policy", missing_ok=False)

    return KeywordPolicy(
        target_keyword_count=positive_int(
            data.get("target_keyword_count"),
            default=20,
        ),
        selection_mix=normalize_selection_mix(data.get("selection_mix")),
        cooldowns=normalize_int_mapping(data.get("cooldowns"), DEFAULT_COOLDOWNS),
        retire_after_zero_winner_runs=positive_int(
            data.get("retire_after_zero_winner_runs"),
            default=5,
        ),
        diversity=normalize_diversity(data.get("diversity")),
        banned_terms=normalize_terms(data.get("banned_terms")),
        default_cluster=clean_text(data.get("default_cluster"), "dummy"),
        path=policy_path,
    )


def resolve_policy_path(path: str | Path | None) -> Path:
    if path is None:
        return DEFAULT_POLICY_PATH

    candidate = Path(path)
    if candidate.is_absolute():
        return candidate

    return PROJECT_ROOT / candidate


def normalize_selection_mix(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return dict(DEFAULT_SELECTION_MIX)

    mix = {
        bucket: non_negative_float(raw.get(bucket), default)
        for bucket, default in DEFAULT_SELECTION_MIX.items()
    }
    total = sum(mix.values())
    if total <= 0:
        return dict(DEFAULT_SELECTION_MIX)

    return {bucket: value / total for bucket, value in mix.items()}


def normalize_int_mapping(raw: Any, defaults: dict[str, int]) -> dict[str, int]:
    if not isinstance(raw, dict):
        return dict(defaults)

    return {
        key: positive_int(raw.get(key), default=default)
        for key, default in defaults.items()
    }


def normalize_diversity(raw: Any) -> dict[str, float | int]:
    if not isinstance(raw, dict):
        return dict(DEFAULT_DIVERSITY)

    return {
        "max_share_per_origin": bounded_share(
            raw.get("max_share_per_origin"),
            DEFAULT_DIVERSITY["max_share_per_origin"],
        ),
        "max_share_per_cluster": bounded_share(
            raw.get("max_share_per_cluster"),
            DEFAULT_DIVERSITY["max_share_per_cluster"],
        ),
        "max_per_parent_keyword": positive_int(
            raw.get("max_per_parent_keyword"),
            default=int(DEFAULT_DIVERSITY["max_per_parent_keyword"]),
        ),
    }


def normalize_terms(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()

    terms = []
    for item in raw:
        term = clean_text(item, "")
        if not term:
            continue
        terms.append(term.lower())

    return tuple(sorted(set(terms)))


def bounded_share(raw: Any, default: float | int) -> float:
    value = non_negative_float(raw, float(default))
    if value <= 0:
        return float(default)
    if value > 1:
        return 1.0
    return value
