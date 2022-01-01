import json

from matplotlib import cm


def palette_as_rgba(name):
    _COLOR_STEPS = 100
    _COLOR_MAP = cm.get_cmap(name, _COLOR_STEPS)

    steps = []
    for i in range(_COLOR_STEPS + 1):
        r, g, b, _ = _COLOR_MAP(i)
        steps.append(f"{int(r * 255)},{int(g * 255)},{int(b * 255)}")
    return steps


print(json.dumps(palette_as_rgba("RdYlGn")))
