"""Unit tests for footage query diversity helpers."""

import pipeline.footage as footage


def test_query_bucket_groups_near_duplicate_generic_queries():
    a = footage._query_bucket("person counting cash money hands closeup")
    b = footage._query_bucket("young person counting cash at desk")
    assert a == b


def test_query_bucket_keeps_topic_tokens_when_present():
    debt = footage._query_bucket("credit card debt stress anxiety")
    tax = footage._query_bucket("tax refund check money excited")
    assert debt != tax
