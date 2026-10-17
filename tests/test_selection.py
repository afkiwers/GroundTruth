"""A signal path from a KiCad selection (analysis/selection.py): one bit of each net, in any order, gives the path,
the op-amps' models and the GT labels for the schematic."""
import pytest

from boardgen import opamp_path_board
from groundtruth.adapters.file_source import loads_board
from groundtruth.analysis.selection import path_from_selection
from groundtruth.config.settings import Config


def _board(**kw):
    """U1.1 (SIG_A) → R1 33 Ω → SIG_B → op-amp U5 (+IN 3, OUT 1) → SIG_C → R2 4.7 kΩ → SIG_D → U2.1, no labels."""
    return loads_board(opamp_path_board(r_after="4k7", **kw).text(), "sel.kicad_pcb")


def test_one_bit_of_each_net_in_any_order_gives_the_whole_path():
    """The nets come in any order; the op-amp's input and output pins give the direction; the 4.7 kΩ resistor
    (no signal part by default) goes into Pass through; U5 becomes an amplifier with its pins; the labels are
    those a user would type into the symbols."""
    board = _board()
    r = path_from_selection(board, Config(), ["SIG_C", "SIG_A", "SIG_D", "SIG_B"])
    assert not r.error, r.error
    s = r.spec
    assert (s.driver, s.receivers, s.through, s.name) == ("U1.1", ["U2.1"], ["R2"], "SIG_D")
    assert s.source == "auto" and not s.reviewed
    assert r.models["U5"].kind == "amplifier" and r.models["U5"].pins == ["3", "1"]
    assert r.labels == {"U1": "sig_drv:1", "U5": "amp_in:3, amp_out:1", "U2": "sig_rx:1"}
    assert "U1.1 → R1 → U5 → R2 → U2.1" in r.notes[0]


def test_existing_labels_are_kept_and_an_op_amps_own_signal_label_goes():
    """A symbol's GT labels stay (a converter label on the receiver is untouched, nothing twice); a signal label
    on the op-amp itself would cut the path there, so it is replaced by the stage's labels."""
    g = opamp_path_board(r_after="4k7")
    g.label("U2", "conv1_out, sig_rx:1")
    g.label("U5", "sig_drv:1")
    board = loads_board(g.text(), "sel.kicad_pcb")
    r = path_from_selection(board, Config(), ["SIG_A", "SIG_B", "SIG_C", "SIG_D"])
    assert not r.error, r.error
    assert r.labels == {"U1": "sig_drv:1", "U5": "amp_in:3, amp_out:1"}      # U2 already says it


def test_without_an_op_amp_the_pin_types_give_the_direction():
    """SIG_A and SIG_B joined by R1: the ends are U1.1 (untyped) and U5's +IN (an input): U1 drives."""
    r = path_from_selection(_board(), Config(), ["SIG_B", "SIG_A"])
    assert not r.error, r.error
    assert (r.spec.driver, r.spec.receivers, r.spec.through) == ("U1.1", ["U5.3"], [])
    assert not r.models


@pytest.mark.parametrize("nets, says", [
    (["SIG_A"], "at least two nets"),
    (["SIG_A", "SIG_D"], "no part joins"),
])
def test_a_selection_that_is_no_single_path_says_why(nets, says):
    """One net, or nets no part joins: an error for the user, nothing else."""
    r = path_from_selection(_board(), Config(), nets)
    assert says in r.error and r.spec is None and not r.labels
