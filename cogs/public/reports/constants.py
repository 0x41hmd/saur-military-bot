from typing import List
import logging

from utils.settings import (
    RANK_ORDER,
    AF_RANK_ORDER,
)


logger = logging.getLogger(__name__)


_RANK_INDEX    = {rank: idx for idx, rank in enumerate(RANK_ORDER)}


_AF_RANK_INDEX = {rank: idx for idx, rank in enumerate(AF_RANK_ORDER)}


def escape_markdown(text: str) -> str:
    if not text:
        return text
    return text.replace('*', '\\*').replace('_', '\\_').replace('`', '\\`').replace('~', '\\~')


def _sort_members_by_rank(member_ids: List[int], member_dict: dict, use_af_rank: bool = False) -> List[str]:
    entries = []
    for uid in member_ids:
        member = member_dict.get(uid)
        if not member:
            continue
        if use_af_rank:
            af_rank  = member.get('af_rank', 'None')
            sort_key = _AF_RANK_INDEX.get(af_rank, len(AF_RANK_ORDER)) if af_rank and af_rank != 'None' else len(AF_RANK_ORDER)
        else:
            sort_key = _RANK_INDEX.get(member.get('rank', 'Cadet'), len(RANK_ORDER))
        entries.append((sort_key, escape_markdown(member.get('ig_name', 'Unknown'))))
    entries.sort(key=lambda x: x[0])
    return [name for _, name in entries]


OPERATION_REQUIREMENTS = {
    "bank_ops":              {"min_leaders": 0, "min_participants": 0, "min_total_attendance": 1, "requires_airforce": False},
    "casino_ops":            {"min_leaders": 0, "min_participants": 0, "min_total_attendance": 1, "requires_airforce": False},
    "securicar_ops":         {"min_leaders": 0, "min_participants": 0, "min_total_attendance": 1, "requires_airforce": False},
    "andromada_ops":         {"min_leaders": 0, "min_participants": 0, "min_total_attendance": 1, "requires_airforce": True},
    "andromada_terrors_ops": {"min_leaders": 0, "min_participants": 0, "min_total_attendance": 1, "requires_airforce": True},
    "assault_ops":           {"min_leaders": 0, "min_participants": 0, "min_total_attendance": 1, "requires_airforce": False},
    "warzone":               {"min_leaders": 0, "min_participants": 0, "min_total_attendance": 1, "requires_airforce": True},
    "doomsday":              {"min_leaders": 0, "min_participants": 0, "min_total_attendance": 1, "requires_airforce": False},
    "training":              {"min_leaders": 1, "min_participants": 2, "min_total_attendance": 3, "requires_airforce": False},
    "patrol":                {"min_leaders": 1, "min_participants": 2, "min_total_attendance": 3, "requires_airforce": False},
    "ammo_transport":        {"min_leaders": 0, "min_participants": 1, "min_total_attendance": 1, "requires_airforce": True},
    "extra_points":          {"min_leaders": 0, "min_participants": 1, "min_total_attendance": 1, "requires_airforce": False},
    "af_warzone":            {"min_leaders": 0, "min_participants": 1, "min_total_attendance": 1, "requires_airforce": False},
    "af_counter":            {"min_leaders": 0, "min_participants": 1, "min_total_attendance": 1, "requires_airforce": False},
    "af_training":           {"min_leaders": 1, "min_participants": 2, "min_total_attendance": 3, "requires_airforce": False},
}


EXTRA_POINTS_VALUE = 1


GLOBAL_OP_POINTS = {
    "bank_ops": 1, "casino_ops": 1, "securicar_ops": 1,
    "andromada_ops": 1, "andromada_terrors_ops": 1, "assault_ops": 1,
    "warzone": 4, "doomsday": 2, "training": 3, "patrol": 2, "ammo_transport": 2,
}


AF_OP_POINTS = {"af_warzone": 4, "af_counter": 1, "af_training": 3}


LEADERSHIP_POINTS = {
    "bank_ops": 0, "casino_ops": 0, "securicar_ops": 0,
    "andromada_ops": 0, "andromada_terrors_ops": 0, "assault_ops": 0,
    "warzone": 3, "doomsday": 2, "training": 2, "patrol": 2,
    "ammo_transport": 1, "af_warzone": 2, "af_training": 2,
}


AF_OPS           = {"af_warzone", "af_counter", "af_training"}


WARZONE_RESULTS  = ["4-0", "3-1", "2-2", "1-3", "0-4"]


STANDARD_RESULTS = ["Success", "Failure"]


AF_WZ_RESULTS    = ["Success", "Draw", "Failure"]


def calculate_points(operation_type: str, is_af, is_leader: bool, in_airforce_field: bool) -> tuple:
    is_af = bool(is_af)
    promo_points = af_points = leadership_points = 0
    if is_af and operation_type in AF_OP_POINTS:
        af_points = AF_OP_POINTS.get(operation_type, 0)
    else:
        promo_points = GLOBAL_OP_POINTS.get(operation_type, 1)
    if is_leader:
        leadership_points = LEADERSHIP_POINTS.get(operation_type, 0)
    requires_af = OPERATION_REQUIREMENTS.get(operation_type, {}).get('requires_airforce', False)
    is_ct_andromada = operation_type == "andromada_terrors_ops"
    if in_airforce_field and is_af and (requires_af or is_ct_andromada):
        af_points += 1
    return (promo_points, af_points, leadership_points)
