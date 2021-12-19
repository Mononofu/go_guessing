from concurrent.futures import Future
import json
import os
import subprocess
import tarfile
import threading

import pyspiel
import sgfmill.sgf


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
                # Must be passed as a string, not a tuple.
                move = "(%d, %d)" % (action // board_size, action % board_size)

            q = self._make_query(
                board_size, komi, moves=history + [(to_play, move)], visits=30
            )
            results_by_move[move] = self._run(q)

        # Run a long search for the overall position.
        overall = self._run(
            self._make_query(board_size, komi, moves=history, visits=1600)
        )

        for move, f in sorted(results_by_move.items()):
            print(move, 1 - f.result()["rootInfo"]["winrate"])

        print("overall", overall.result()["rootInfo"]["winrate"])

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

        print(game.get_size(), game.get_komi(), game.get_handicap())
        print([m.get_move() for m in game.get_main_sequence()[1:]])

        history = []

        analyser.analyse(state, history, board_size, game.get_komi())

        break

        for node in game.get_main_sequence()[1:]:
            color, point = node.get_move()
            # See https://github.com/deepmind/open_spiel/blob/master/open_spiel/games/go.h#L67
            if point is None:
                action = board_size ** 2
                history.append((color, "pass"))
            else:
                row, col = point
                action = row * board_size + col
                history.append((color, (row, col)))

            state.apply_action(action)
            analyser.analyse(state, history, board_size, game.get_komi())
