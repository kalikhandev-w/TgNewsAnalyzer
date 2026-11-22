"""Чистый движок скоринга: комбинированная формула и жадное назначение."""

W_VECTOR = 0.60
W_NER = 0.25
W_KEYWORDS = 0.15
TIME_PENALTY_HOURS = 24
TIME_PENALTY_FACTOR = 0.8


def combined_score(vector, ner, keywords, time_diff_hours):
    """Комбинированный score пары статья↔пост со штрафом за время."""
    score = W_VECTOR * vector + W_NER * ner + W_KEYWORDS * keywords
    if time_diff_hours > TIME_PENALTY_HOURS:
        score *= TIME_PENALTY_FACTOR
    return score


def greedy_assign(candidates):
    """Жадное назначение по убыванию score.

    candidates: list of (row_idx, post_key, score).
    Возвращает {row_idx: post_key}: один пост — одной строке, один на строку.
    """
    ordered = sorted(candidates, key=lambda c: c[2], reverse=True)
    assigned_rows = set()
    assigned_posts = set()
    result = {}
    for row_idx, post_key, _score in ordered:
        if row_idx in assigned_rows or post_key in assigned_posts:
            continue
        result[row_idx] = post_key
        assigned_rows.add(row_idx)
        assigned_posts.add(post_key)
    return result
