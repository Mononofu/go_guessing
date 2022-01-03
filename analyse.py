import base64
from concurrent.futures import Future
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import threading
from typing import Mapping, NamedTuple, Tuple

from matplotlib import cm
import pyspiel
from rich.console import Console
from rich.text import Text
import sgfmill.sgf
import tqdm

console = Console()

COLUMNS = "ABCDEFGHJKLMNOPQRST"

INDEX_PATH = "analysed/index.json"


class RGBA(NamedTuple):
    red: int
    green: int
    blue: int
    alpha: int = 255

    @property
    def hex(self) -> str:
        return f"#{self.red:02x}{self.green:02x}{self.blue:02x}"

    def blend(self, back: "RGBA") -> "RGBA":
        return RGBA(
            alpha=(255 - (255 - back.alpha) * (255 - self.alpha) // 255),
            red=(back.red * (255 - self.alpha) + self.red * self.alpha) // 255,
            green=(back.green * (255 - self.alpha) + self.green * self.alpha) // 255,
            blue=(back.blue * (255 - self.alpha) + self.blue * self.alpha) // 255,
        )


def decode_gtp_move(move: str) -> Tuple[int, int]:
    row = int(move[1:]) - 1
    col = COLUMNS.find(move[0])
    return (row, col)


def encode_gtp_move(row: int, col: int) -> str:
    return COLUMNS[col] + str(row + 1)


_COLOR_STEPS = 100
_COLOR_MAP = cm.get_cmap("RdYlGn", _COLOR_STEPS)


def delta_as_color(delta: float, max_delta: float) -> RGBA:
    x = (delta + max_delta) / max_delta
    r, g, b, _ = _COLOR_MAP(int(x * _COLOR_STEPS))
    return RGBA(
        int(r * 255),
        int(g * 255),
        int(b * 255),
        alpha=max(0, min(255, 75 + int(180 * x))),
    )


class Analyser:
    def __init__(self, katago_path: str, model_path: str):
        self._query_id = 0
        self._outstanding = {}
        self._p = subprocess.Popen(
            [
                katago_path,
                "analysis",
                "-config",
                os.path.join(os.path.dirname(__file__), "analysis.cfg"),
                "-model",
                model_path,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            encoding="utf-8",
        )
        threading.Thread(target=self._katago_reader, daemon=True).start()

        info = self._run(dict(id="init", action="query_version")).result()
        print(info["version"], info["git_hash"])

    def __del__(self):
        self._p.kill()
        self._p.wait()

    def analyse(self, state, history, board_size: int, komi: float) -> float:
        # Analyse the top moves by visit count, as well as all moves close to
        # the current winrate.
        top_n = 8
        max_delta = 0.03

        if history:
            last_player = history[-1][0]
            to_play = "b" if last_player == "w" else "w"
        else:
            to_play = "b"

        # Run a short search for every possible move.
        results_by_move = {}
        for action in state.legal_actions():
            if action == board_size ** 2:
                move = "pass"
            else:
                move = encode_gtp_move(action // board_size, action % board_size)

            q = self._make_query(
                board_size, komi, moves=history + [(to_play, move)], visits=25
            )
            results_by_move[move] = self._run(q)

        # Run a long search for the overall position.
        overall = self._run(
            self._make_query(board_size, komi, moves=history, visits=1600)
        ).result()
        overall_winrate = overall["rootInfo"]["winrate"]

        move_infos = sorted(overall["moveInfos"], key=lambda i: i["order"])
        to_analyse = [i["move"] for i in move_infos[:top_n]]

        if overall_winrate > 0.1 and overall_winrate < 0.9:
            # Only select moves according to value difference if the game isn't
            # won/lost already.
            for move, f in results_by_move.items():
                res = f.result()
                winrate = 1 - res["rootInfo"]["winrate"]
                if winrate > overall_winrate - max_delta:
                    to_analyse.append(move)

        # Replace the results for the most visited / highest value moves with a
        # more detailed analysis.
        for move in set(to_analyse):
            q = self._make_query(
                board_size, komi, moves=history + [(to_play, move)], visits=200
            )
            results_by_move[move] = self._run(q)

        results_by_move = {k: v.result() for k, v in results_by_move.items()}
        self._print_results(state, history, overall_winrate, results_by_move)
        return overall_winrate, results_by_move

    def _print_results(self, state, history, overall_winrate: float, results_by_move):
        max_delta = 0.1
        bg_color = RGBA(191, 153, 42)
        console.print("Move %3d - winrate: %.3f" % (len(history), overall_winrate))
        console.print()
        rows = str(state).strip().split("\n")[2:]
        for i, row in enumerate(rows):
            row_i = board_size - i
            if row_i == 0:
                break

            text = Text("%2d " % row_i)

            row = row.strip().split(" ")[1]
            for col, stone in enumerate(row):
                bg = bg_color
                if history and encode_gtp_move(row_i - 1, col) == history[-1][1]:
                    bg = RGBA(77, 172, 255)
                if stone == "X":
                    text.append("⚫", style=f"black on {bg.hex}")
                elif stone == "O":
                    text.append("⚪", style=f"white on {bg.hex}")
                else:
                    move = encode_gtp_move(row_i - 1, col)
                    if move in results_by_move:
                        # Color based on change in winrate if the move were played.
                        winrate = 1 - results_by_move[move]["rootInfo"]["winrate"]
                        # Delta will usually be negative, and at most -1, but we really only
                        # care about the range of [0, -0.2].
                        delta = winrate - overall_winrate
                        bg = delta_as_color(delta, max_delta).blend(bg)

                    if row_i in [4, 10, 16] and col in [3, 9, 15]:
                        text.append("• ", style=f"black on {bg.hex}")
                    else:
                        text.append("  ", style=f"white on {bg.hex}")
            console.print(text)
        console.print("   " + " ".join(COLUMNS))
        console.print()
        console.print(" Δ winrate: ")
        scale = Text(f"-{max_delta:.1f} ")
        width = 2 * board_size
        for i in range(width + 1):
            bg = delta_as_color(max_delta * (i - width) / width, max_delta).blend(bg)
            scale.append(" ", style=f"white on {bg.hex}")
        scale.append(" +0")
        console.print(scale)
        console.print()
        console.print()

    def _make_query(self, board_size, komi, moves, visits):
        self._query_id += 1
        return {
            "id": f"query_{self._query_id}",
            "moves": moves,
            "rules": "tromp-taylor",
            "komi": komi,
            "boardXSize": board_size,
            "boardYSize": board_size,
            "maxVisits": visits,
        }

    def _run(self, query):
        f = Future()
        self._outstanding[query["id"]] = (f, query)
        json.dump(query, self._p.stdin)
        self._p.stdin.write("\n")
        self._p.stdin.flush()
        return f

    def _katago_reader(self):
        while self._p.poll() is None:
            line = self._p.stdout.readline()
            try:
                res = json.loads(line)
            except json.JSONDecodeError:
                print("Failed to parse KataGo result: ", line)
                sys.exit(1)

            (f, query) = self._outstanding.pop(res["id"])
            if "error" in res:
                msg = res["error"]
                if "field" in res:
                    msg += f" in field '{res['field']}'"
                f.set_exception(ValueError(msg + f" for {query}"))
            elif "warning" in res:
                f.set_exception(
                    ValueError(
                        f"{res['warning']} in field '{res['field']}' for {query}"
                    )
                )
            else:
                f.set_result(res)


def encode_results(analysed, winrate: float, results_by_move):
    analysed["winrate"].append(round(winrate, 4))

    size = analysed["board_size"]
    encoded = b""
    for row in range(size):
        for col in range(size):
            move = encode_gtp_move(row, col)
            if move in results_by_move:
                q = 1 - results_by_move[move]["rootInfo"]["winrate"]
                assert q >= 0 and q <= 1, q
                val = int(q * (2 ** 16 - 2)) + 1
            else:
                val = 0
            encoded += bytes([val // 256, val % 256])
    analysed["q"].append(base64.b64encode(encoded).decode("utf-8"))


def strip_variations(game: sgfmill.sgf.Sgf_game):
    cur = game.root
    while cur:
        # Only keep the left-most (main) variation at each move.
        for node in cur[1:]:
            node.delete()
        cur = cur[0]


def strip_comments(game: sgfmill.sgf.Sgf_game):
    for node in game.get_main_sequence():
        for prop in ["C", "LB", "TR", "SQ", "MA", "CR"]:
            if node.has_property(prop):
                node.unset(prop)


def load_index():
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH) as f:
            return json.load(f)
    return {}


analyser = Analyser(
    "/home/mononofu/katago/katago",
    "/home/mononofu/katago/kata1-b40c256-s10499183872-d2559211369.bin.gz",
)
print("\n" * 6)

games_analysed = set(load_index().keys())

with tarfile.open("sgfs/games.tgz") as tar:
    for name in tqdm.tqdm(tar.getnames()):
        if not name.endswith(".sgf"):
            continue

        sgf = tar.extractfile(name).read()
        game = sgfmill.sgf.Sgf_game.from_bytes(sgf)
        strip_variations(game)
        strip_comments(game)

        if game.get_handicap() is not None:
            continue

        board_size = game.get_size()
        state = pyspiel.load_game(f"go(board_size={board_size})").new_initial_state()
        history = []

        analysed = {
            "sgf": game.serialise(wrap=None).decode("utf-8"),
            "board_size": board_size,
            "winrate": [],
            "q": [],
        }
        sgf_hash = hashlib.sha256(analysed["sgf"].encode("utf-8")).hexdigest()
        analysed["sgf_hash"] = sgf_hash

        if sgf_hash in games_analysed:
            continue

        print("analysing", name, sgf_hash, "\n")

        winrate, results_by_move = analyser.analyse(
            state, history, board_size, game.get_komi()
        )
        encode_results(analysed, winrate, results_by_move)

        for node in game.get_main_sequence()[1:]:
            color, point = node.get_move()
            # See https://github.com/deepmind/open_spiel/blob/master/open_spiel/games/go.h#L67
            if point is None:
                action = board_size ** 2
                history.append((color, "pass"))
            else:
                row, col = point
                action = row * board_size + col
                history.append((color, encode_gtp_move(row, col)))

            state.apply_action(action)

            winrate, results_by_move = analyser.analyse(
                state, history, board_size, game.get_komi()
            )
            encode_results(analysed, winrate, results_by_move)
            if winrate < 0.05 or winrate > 0.95:
                break

        with open(f"analysed/{sgf_hash}.json", "w") as f:
            json.dump(analysed, f, sort_keys=True)

        index = load_index()
        index[sgf_hash] = dict(
            name=name,
            analysed_moves=len(analysed["q"]),
            total_moves=len(game.get_main_sequence()),
        )
        with open(INDEX_PATH, "w") as f:
            json.dump(index, f, sort_keys=True, indent=2)
        games_analysed.add(sgf_hash)
