from __future__ import annotations

from dataclasses import dataclass

from django.core.paginator import Paginator


@dataclass(frozen=True)
class PaginationResult:
    page_obj: object
    paginator: Paginator
    is_paginated: bool
    page_range: list[int]


def paginate(request, queryset, per_page: int = 20, page_param: str = "page") -> PaginationResult:
    try:
        per_page_n = int(per_page or 20)
    except Exception:
        per_page_n = 20
    if per_page_n <= 0:
        per_page_n = 20

    paginator = Paginator(queryset, per_page_n)
    page_number = request.GET.get(page_param) or "1"
    page_obj = paginator.get_page(page_number)

    try:
        cur = int(page_obj.number)
        total = int(paginator.num_pages)
        start = max(1, cur - 2)
        end = min(total, cur + 2)
        page_range = list(range(start, end + 1))
    except Exception:
        page_range = []

    return PaginationResult(
        page_obj=page_obj,
        paginator=paginator,
        is_paginated=bool(paginator.num_pages and paginator.num_pages > 1),
        page_range=page_range,
    )

