"""The IPC adapter, tested with real kicad-python objects on a fake board (no KiCad needed)."""
import os

import pytest

kipy = pytest.importorskip("kipy")

from kipy.board_types import BoardSegment, Track, Via, Zone  # noqa: E402
from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode, Vector2  # noqa: E402
from kipy.proto.board import board_pb2  # noqa: E402
from kipy.proto.board.board_types_pb2 import BoardLayer, ZoneType  # noqa: E402

from groundtruth.adapters.kipy_source import read_board  # noqa: E402

MM = 1_000_000


def v(x, y):
    return Vector2.from_xy(int(x * MM), int(y * MM))


def rect(x0, y0, x1, y1):
    pwh = PolygonWithHoles()
    pl = PolyLine()
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        pl.append(PolyLineNode.from_point(v(x, y)))
    pl.closed = True
    pwh.outline = pl
    return pwh


class FakeBoard:
    name = "fake.kicad_pcb"

    def __init__(self):
        t = Track()
        t.start, t.end, t.width, t.layer = v(0, 0), v(10, 0), int(0.2 * MM), BoardLayer.BL_F_Cu
        t.proto.net.name = "SIG"
        self.tracks = [t]
        via = Via()
        via.position = v(10, 0)
        via.proto.net.name = "SIG"
        via.padstack.drill.start_layer = BoardLayer.BL_F_Cu
        via.padstack.drill.end_layer = BoardLayer.BL_B_Cu
        via.padstack.drill.diameter = v(0.3, 0.3)
        self.vias = [via]
        z = Zone()
        z.proto.type = ZoneType.ZT_COPPER
        z.proto.layers.append(BoardLayer.BL_In1_Cu)
        z.proto.copper_settings.net.name = "GND"
        fill = z.proto.filled_polygons.add()
        fill.layer = BoardLayer.BL_In1_Cu
        fill.shapes.polygons.append(rect(-5, -5, 15, 5).proto)
        self.zones = [z]
        edges = []
        for (a, b) in (((-5, -5), (15, -5)), ((15, -5), (15, 5)), ((15, 5), (-5, 5)), ((-5, 5), (-5, -5))):
            s = BoardSegment()
            s.layer = BoardLayer.BL_Edge_Cuts
            s.start, s.end = v(*a), v(*b)
            edges.append(s)
        self.shapes = edges

    def get_stackup(self):
        st = board_pb2.BoardStackup()
        for layer, t, kind in ((BoardLayer.BL_F_Cu, 35_000, "cu"), (None, 200_000, "d"),
                               (BoardLayer.BL_In1_Cu, 35_000, "cu"), (None, 1_000_000, "d"),
                               (BoardLayer.BL_In2_Cu, 35_000, "cu"), (None, 200_000, "d"),
                               (BoardLayer.BL_B_Cu, 35_000, "cu")):
            item = st.layers.add()
            item.enabled = True
            item.thickness.value_nm = t
            if kind == "cu":
                item.layer = layer
                item.type = board_pb2.BoardStackupLayerType.BSLT_COPPER
            else:
                item.layer = BoardLayer.BL_UNDEFINED
                item.type = board_pb2.BoardStackupLayerType.BSLT_DIELECTRIC
                sub = item.dielectric.layer.add()
                sub.epsilon_r = 4.2
        from kipy.board import BoardStackup
        return BoardStackup(st)

    def get_copper_layer_count(self):
        return 4

    def get_tracks(self):
        return self.tracks

    def get_vias(self):
        return self.vias

    def get_footprints(self):
        return []

    def get_zones(self):
        return self.zones

    def get_shapes(self):
        return self.shapes

    def get_pad_shapes_as_polygons(self, pads, layer):
        return [None for _ in pads]


def test_read_board_from_kipy_objects():
    b = read_board(FakeBoard())
    assert b.copper_layers == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert b.stackup.distance("F.Cu", "In1.Cu") == pytest.approx(0.235)
    assert b.stackup.dielectrics[0].epsilon_r == pytest.approx(4.2)
    t = b.tracks[0]
    assert (t.start, t.end, t.width, t.layer, t.net) == ((0, 0), (10, 0), 0.2, "F.Cu", "SIG")
    assert b.vias[0].layers == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert b.zones[0].net == "GND" and b.zones[0].filled["In1.Cu"][0][0][2] == (15.0, 5.0)
    assert len(b.outline) == 1


class LiveStackupBoard(FakeBoard):
    """What KiCad 10.0.5 actually sends: every stackup item 'disabled', dielectrics without materials."""

    def get_stackup(self):
        st = super().get_stackup()
        for item in st.proto.layers:
            item.enabled = False
            del item.dielectric.layer[:]
        return st


def test_live_stackup_keeps_thicknesses_and_warns_without_materials():
    b = read_board(LiveStackupBoard())
    assert b.stackup.distance("F.Cu", "In1.Cu") == pytest.approx(0.235)       # not an even split of 1.6 mm
    assert b.stackup.distance("In1.Cu", "In2.Cu") == pytest.approx(1.035)
    assert b.stackup.dielectrics[0].epsilon_r == pytest.approx(4.5)            # FR4 assumed ...
    assert any("dielectric materials" in w for w in b.warnings)                # ... and said so


def test_stackup_only_reader_matches_the_full_parser(tmp_path):
    from boardgen import layer_change_board

    from groundtruth.adapters.file_source import load_board, load_stackup
    path = layer_change_board(None).write(tmp_path / "s.kicad_pcb")
    full, head = load_board(path).stackup, load_stackup(path)
    assert [vars(c) for c in head.copper] == [vars(c) for c in full.copper]
    assert [vars(d) for d in head.dielectrics] == [vars(d) for d in full.dielectrics]


class BusyBoard(FakeBoard):
    """KiCad answers 'busy' (a dialog is open) for the first `busy` requests."""

    def __init__(self, busy):
        super().__init__()
        self.busy = busy

    def get_tracks(self):
        from kipy.errors import ApiError
        from kipy.proto.common import ApiStatusCode
        if self.busy > 0:
            self.busy -= 1
            raise ApiError("KiCad is busy and cannot respond to API requests right now", code=ApiStatusCode.AS_BUSY)
        return self.tracks


def test_read_board_waits_while_kicad_is_busy():
    b = read_board(BusyBoard(busy=2))
    assert b.tracks and b.tracks[0].net == "SIG"


def test_busy_kicad_gives_a_clear_error():
    from groundtruth.adapters.kipy_source import KiCadBusyError, when_ready
    from groundtruth.adapters.kipy_sink import clear
    board = BusyBoard(busy=10 ** 6)
    with pytest.raises(KiCadBusyError, match="close any open dialog"):
        when_ready(board.get_tracks, wait=0.5)

    class BusyGroups:
        def get_groups(self):
            return board.get_tracks()
    with pytest.raises(KiCadBusyError):
        clear(BusyGroups(), wait=0.3)          # every sink call retries, then explains


class FillingBoard:
    """A board whose zone fill keeps KiCad busy: refill_zones starts it (block must be False: kipy's own wait stops
    at the first ping answered), then `busy` board requests are refused, one more times out in the transport."""

    def __init__(self, busy: int):
        """busy: the board requests refused (AS_BUSY) while the fill runs."""
        self.busy, self.started, self.timeout, self.asked = busy, [], True, 0

    def refill_zones(self, block=True):
        """Start the fill (record how it was asked)."""
        self.started.append(block)

    def get_copper_layer_count(self):
        """Refused while filling, one transport timeout, then answered."""
        from kipy.errors import ApiError
        from kipy.proto.common import ApiStatusCode
        self.asked += 1
        if self.busy > 0:
            self.busy -= 1
            raise ApiError("KiCad is busy", code=ApiStatusCode.AS_BUSY)
        if self.timeout:
            self.timeout = False
            raise TimeoutError("no answer within the transport's timeout")
        return 6


def test_a_refill_waits_until_kicad_has_filled(monkeypatch):
    """The user, on a complex board: "pressing refill zones causes this error" (KiCad busy: close any dialog). The
    fill is started without kipy's ping wait, then the board is asked until it answers: busy and a transport timeout
    are waited through, with progress said; past the wait, an error that says KiCad is still filling."""
    from groundtruth.adapters import kipy_sink
    from groundtruth.adapters.kipy_source import KiCadBusyError
    monkeypatch.setattr(kipy_sink, "REFILL_POLL_S", 0.01)
    board, said = FillingBoard(busy=400), []
    kipy_sink.refill_zones(board, lambda text, frac: said.append(text))
    assert board.started == [False] and board.asked == 402 and board.busy == 0
    assert said and said[-1].startswith("KiCad is filling the zones…")
    with pytest.raises(KiCadBusyError, match="still filling the zones"):
        kipy_sink.refill_zones(FillingBoard(busy=10 ** 6), wait=0.2)


def test_other_api_errors_are_not_retried():
    from kipy.errors import ApiError
    from groundtruth.adapters.kipy_source import when_ready
    calls = []

    def fails():
        calls.append(1)
        raise ApiError("bad request")
    with pytest.raises(ApiError):
        when_ready(fails)
    assert len(calls) == 1


@pytest.mark.skipif(not os.environ.get("GROUNDTRUTH_LIVE"), reason="set GROUNDTRUTH_LIVE=1 with KiCad running")
def test_live_kicad_board():
    from groundtruth.adapters.kipy_source import connect
    board = read_board(connect().get_board())
    assert board.copper_layers


def test_selected_nets_from_the_kicad_selection():
    """The nets of what is selected in KiCad, in selection order and once each; items without a net
    (footprints, text, an unconnected pad, a zone without a net) are passed over."""
    pytest.importorskip("kipy")
    from types import SimpleNamespace as NS
    from kipy.board_types import Net
    from groundtruth.adapters.kipy_source import selected_nets
    track, via, unconnected = Track(), Via(), Track()
    track.net, via.net = Net(name="/CPU/SPI_SCK"), Net(name="/CPU/SPI_SCK")   # unconnected: an empty net
    items = [track, NS(net=NS(name="/CPU/SPI_MOSI")), via, NS(reference="U1"), NS(net=None), unconnected]
    assert selected_nets(NS(get_selection=lambda: items)) == ["/CPU/SPI_SCK", "/CPU/SPI_MOSI"]
    from groundtruth.adapters.kipy_source import selection_kinds
    assert selection_kinds(NS(get_selection=lambda: [via, NS(reference="U1")])) == ["Via", "SimpleNamespace"]




class ShowBoard:
    """A PCB editor stand-in that records what show_items asks for: selection, actions, active layer. `busy` makes
    the first requests answer AS_BUSY; `busy_action` makes that RunAction answer AS_BUSY once (part-way through)."""

    def __init__(self, known, unknown_actions=(), busy=0, busy_action="", bad_layer=False, active=2):
        from kipy.proto.common.types import DocumentSpecifier
        self.document = DocumentSpecifier()
        self.known, self.unknown_actions, self.busy, self.bad_layer = set(known), set(unknown_actions), busy, bad_layer
        self.busy_action, self.active = busy_action, active
        self.selection, self.log = [], []
        self.client = self

    def _maybe_busy(self):
        from kipy.errors import ApiError
        from kipy.proto.common import ApiStatusCode
        if self.busy:
            self.busy -= 1
            raise ApiError("KiCad is busy and cannot respond to API requests right now", code=ApiStatusCode.AS_BUSY)

    def clear_selection(self):
        self._maybe_busy()
        self.selection = []
        self.log.append("clear")

    def send(self, cmd, response_type):
        from types import SimpleNamespace as NS
        from kipy.errors import ApiError
        from kipy.proto.common import ApiStatusCode, commands
        if isinstance(cmd, commands.AddToSelection):
            ids = [k.value for k in cmd.items]
            self.selection += [u for u in ids if u in self.known and u not in self.selection]
            self.log.append(("select", ids))
            return NS(items=list(self.selection))
        if isinstance(cmd, commands.RunAction):
            if cmd.action == self.busy_action:
                self.busy_action = ""
                raise ApiError("KiCad is busy", code=ApiStatusCode.AS_BUSY)
            self.log.append(cmd.action)
            return NS(status=commands.RAS_INVALID if cmd.action in self.unknown_actions else commands.RAS_OK)
        raise AssertionError(f"unexpected request {type(cmd).__name__}")

    def get_active_layer(self):
        return self.active

    def set_active_layer(self, layer):
        from kipy.errors import ApiError
        from kipy.proto.common import ApiStatusCode
        if self.bad_layer:
            raise ApiError("layer not enabled", code=ApiStatusCode.AS_BAD_REQUEST)
        self.active = layer
        self.log.append(("layer", layer))


HL, CLR = "pcbnew.EditorControl.highlightNetSelection", "pcbnew.EditorControl.clearHighlight"
FIT, OUT = "common.Control.zoomFitSelection", "common.Control.zoomOutCenter"


def test_show_items_lights_the_issue_net_only_then_zooms_and_adds_the_planes():
    """Only the issue's own copper is selected when KiCad highlights (it lights up every selected net); the rest of
    the focus (a ground stitching via) joins before the zoom, the planes after it; then the reference layer is
    made active and the user's layer reported back. No commit."""
    from kipy.util.board_layer import layer_from_canonical_name
    from groundtruth.adapters.kipy_sink import show_items
    board = ShowBoard(known={"t1", "t2", "gnd-via", "zone"}, active=0)
    r = show_items(board, ["t1", "t2", "gone"], ["t1", "t2", "gone", "gnd-via"], ["zone"], zoom_out=2,
                   layer="In1.Cu")
    assert board.log == ["clear", ("select", ["t1", "t2", "gone"]), HL, ("select", ["gnd-via"]), FIT, OUT, OUT,
                         ("select", ["zone"]), ("layer", layer_from_canonical_name("In1.Cu"))]
    assert r == {"selected": 4, "focus": 3, "lit": True, "previous_layer": 0, "notes": []}


def test_show_items_never_lights_the_mouse_net_and_leaves_the_users_highlight():
    """With none of the issue's copper on the board any more (or only footprints), KiCad would highlight the net
    under the mouse: no highlight then. A highlight of ours from before is cleared; one the user set in KiCad is
    left alone."""
    from groundtruth.adapters.kipy_sink import show_items
    board = ShowBoard(known={"fp"})
    r = show_items(board, ["stale-track"], ["stale-track", "fp"], clear_old=False)
    assert HL not in board.log and CLR not in board.log and not r["lit"] and FIT in board.log
    board = ShowBoard(known={"fp"})
    r = show_items(board, [], ["fp"], clear_old=True)              # ours was on: it goes
    assert board.log[1] == CLR and not r["lit"]
    board = ShowBoard(known=set())
    r = show_items(board, [], ["gone"], ["gone-zone"], layer="In1.Cu")
    assert r["selected"] == 0 and FIT not in board.log and ("layer", 4) not in board.log   # nothing: view kept


def test_show_items_restarts_cleanly_when_kicad_gets_busy_part_way():
    from groundtruth.adapters.kipy_sink import show_items
    calm = ShowBoard(known={"t", "z"})
    expected = show_items(calm, ["t"], ["t"], ["z"], zoom_out=1)
    busy = ShowBoard(known={"t", "z"}, busy_action=FIT)             # busy at the zoom, after the highlight
    r = show_items(busy, ["t"], ["t"], ["z"], zoom_out=1, wait=5)
    assert r == expected and busy.selection == calm.selection
    assert busy.log[-len(calm.log):] == calm.log                     # the retry ran the whole sequence again


def test_show_items_reports_unknown_actions_and_layers_and_restores_a_layer_by_id():
    from groundtruth.adapters.kipy_sink import clear_highlight, show_items
    board = ShowBoard(known={"t"}, unknown_actions={FIT}, busy=2, bad_layer=True)
    r = show_items(board, ["t"], ["t"], zoom_out=2, layer="In7.Cu", wait=5)
    assert r["selected"] == 1 and OUT not in board.log                # no fit: no steps out either
    assert any("zoomFitSelection" in n for n in r["notes"]) and any("In7.Cu" in n for n in r["notes"])
    board = ShowBoard(known=set(), active=4)
    show_items(board, [], [], layer=0)                               # the user's layer given back by id
    clear_highlight(board, layer=31)
    assert ("layer", 0) in board.log and board.log[-2:] == [CLR, ("layer", 31)]


def test_pin_names_from_the_api_lose_the_pin_number_like_the_file_readers():
    """KiCad 10.0.5's API sends a pad's symbol pin as name + '_' + number (Q601 on refboard: 'G_1', 'S_2',
    'D_3'); the file reader strips that suffix and every pin-name rule (gate, SW, VIN, GND) expects it stripped.
    With 'G_1' the surge stopper's FET was not recognised in the plugin and no input connector was found."""
    pytest.importorskip("kipy")
    from groundtruth.adapters.kipy_source import pin_name
    from groundtruth.analysis.detect import GATE_FN
    assert [pin_name(n) for n in ("G_1", "S_2", "D_3", "PA5_12", "V_{OUT}_1", "SW", "", None)] == \
        ["G", "S", "D", "PA5", "V_{OUT}", "SW", "", ""]
    assert GATE_FN.match(pin_name("G_1")) and not GATE_FN.match("G_1")


def test_selected_references_reads_footprints_only_and_does_not_wait_when_polling():
    """The Parts tab follows KiCad's selection: footprints' references once each, in order; tracks and other items
    are passed over; with wait=0 a busy KiCad (a dialog, an interactive tool) gives KiCadBusyError at once."""
    pytest.importorskip("kipy")
    from types import SimpleNamespace as NS
    from kipy.errors import ApiError
    from kipy.proto.common import ApiStatusCode
    from groundtruth.adapters.kipy_source import KiCadBusyError, selected_references

    def fp(ref):
        return NS(reference_field=NS(text=NS(value=ref)))
    items = [fp("Q601"), NS(net=NS(name="GND")), fp("R309"), fp("Q601")]
    assert selected_references(NS(get_selection=lambda: items)) == ["Q601", "R309"]

    def busy():
        raise ApiError("KiCad is busy", code=ApiStatusCode.AS_BUSY)
    import time
    t = time.perf_counter()
    with pytest.raises(KiCadBusyError):
        selected_references(NS(get_selection=busy), wait=0.0)
    assert time.perf_counter() - t < 0.1


def test_schematic_labels_fall_back_when_kicad_cannot_take_them():
    """KiCad 10 has no schematic API (kicad-python marks it KiCad 11; its 0.8 schematic module doesn't even import):
    write_symbol_labels says so with SchematicApiUnavailable, whatever the reason, so the window shows the labels
    instead (the path is in the settings either way)."""
    from kipy.errors import ApiError
    from groundtruth.adapters.kipy_sink import SchematicApiUnavailable, write_symbol_labels

    class Client:
        def send(self, cmd, response_type):
            raise ApiError("no handler available for request of type GetOpenDocuments")

    class Board:
        client = Client()
    with pytest.raises(SchematicApiUnavailable):
        write_symbol_labels(Board(), {"U301": "amp_in:1, amp_out:4"})
