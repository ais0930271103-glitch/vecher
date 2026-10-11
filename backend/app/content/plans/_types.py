from typing import TypedDict


class Option(TypedDict, total=False):
    title: str
    note: str
    items_title: str
    items: list[str]
    steps: list[str]


class Plan(TypedDict):
    intro: str
    options: list[Option]


def O(title: str, note: str = "", items_title: str = "", items: tuple = (), steps: tuple = ()) -> Option:  # noqa: E741
    opt: Option = {"title": title, "note": note}
    if items:
        opt["items_title"] = items_title or "Понадобится"
        opt["items"] = list(items)
    if steps:
        opt["steps"] = list(steps)
    return opt


def P(intro: str, *options: Option) -> Plan:
    return {"intro": intro, "options": list(options)}
