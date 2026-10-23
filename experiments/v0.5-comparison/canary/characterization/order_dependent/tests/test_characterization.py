"""Order-dependent canary suite: the second test reads state the first one records. It passes in file
order and fails in reverse order, which the reproducibility check (G4) must detect."""
from canary import core

RECORDED = {}


def test_a_record():
    RECORDED["label-90"] = core.label(90)
    assert RECORDED["label-90"] == "high"


def test_b_compare():
    assert RECORDED["label-90"] == core.label(90)
