"""Regression coverage for Muse Spark model defaults."""

from src.features.grants.assessor import DEFAULT_GRANTS_LLM_MODEL
from src.features.support.support_cog import SUPPORT_AGENT_MODEL_DEFAULT


def test_muse_spark_defaults_use_1_3_contributor():
    expected = "meta/muse-spark-1.3-contributor"

    assert DEFAULT_GRANTS_LLM_MODEL == expected
    assert SUPPORT_AGENT_MODEL_DEFAULT == expected
