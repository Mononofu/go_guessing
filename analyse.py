from concurrent.futures import Future
import json
import os
import subprocess
import tarfile
import threading

import pyspiel
from rich.console import Console
from rich.text import Text
import sgfmill.sgf

console = Console()

COLUMNS = "ABCDEFGHJKLMNOPQRST"


def decode_gtp_move(move):
    row = int(move[1:]) - 1
    col = COLUMNS.find(move[0])
    return (row, col)


def encode_gtp_move(row, col):
    return COLUMNS[col] + str(row + 1)


class Analyser:
    def __init__(self, katago_path, model_path):
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

    def analyse(self, state, history, board_size, komi):
        # Analyse the top moves by visit count, as well as all moves close to
        # the current winrate.
        top_n = 8
        max_delta = 0.02

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
                board_size, komi, moves=history + [(to_play, move)], visits=30
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

        for f in results_by_move.values():
            f.result()

        console.print("Winrate: %.3f" % overall_winrate)
        rows = str(state).strip().split("\n")[2:]
        for i, row in enumerate(rows):
            row_i = board_size - i
            if row_i == 0:
                break

            text = Text("%2d " % row_i)

            row = row.strip().split(" ")[1]
            for col, stone in enumerate(row):
                if stone == "X":
                    text.append("⬤ ", style="black on #bf992a")
                elif stone == "O":
                    text.append("⬤ ", style="white on #bf992a")
                else:
                    move = encode_gtp_move(row_i - 1, col)
                    if move in results_by_move:
                        # Color based on change in winrate if the move were played.
                        winrate = (
                            1 - results_by_move[move].result()["rootInfo"]["winrate"]
                        )
                        # Delta will usually be negative, and at most -1, but we really only
                        # care about the range of [0, -0.2].
                        delta = winrate - overall_winrate
                        x = (delta + 0.2) * 5
                        # print(overall_winrate, winrate, delta, x)
                        red = int(max(min(2 * (1 - x) * 255, 255), 0))
                        green = int(max(min(2 * x * 255, 255), 0))

                        # Highlight the moves we analysed with more visits using a bigger circle.
                        letter = "⬤" if move in to_analyse else "●"
                        text.append(
                            letter + " ", style=f"rgb({red},{green},0) on #bf992a"
                        )
                    else:
                        text.append("  ", style="white on #bf992a")
            console.print(text)
        console.print("   " + " ".join(COLUMNS))
        console.print()

        return overall_winrate

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
            res = json.loads(self._p.stdout.readline())

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


analyser = Analyser(
    "/home/mononofu/katago/katago",
    "/home/mononofu/katago/kata1-b40c256-s10499183872-d2559211369.bin.gz",
)


with tarfile.open("sgfs/alphago.tgz") as f:
    for name in f.getnames():
        if not name.endswith(".sgf"):
            continue
        sgf = f.extractfile(name).read()
        game = sgfmill.sgf.Sgf_game.from_bytes(sgf)

        if game.get_handicap() is not None:
            continue

        board_size = game.get_size()
        state = pyspiel.load_game(f"go(board_size={board_size})").new_initial_state()
        history = []

        analyser.analyse(state, history, board_size, game.get_komi())

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
            winrate = analyser.analyse(state, history, board_size, game.get_komi())

            if winrate < 0.05 or winrate > 0.95:
                break

        break
