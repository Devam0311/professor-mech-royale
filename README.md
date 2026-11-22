# PROFESSOR

A Mech Royale bot by **Sparsh** for the Builders Buildathon, written with Claude Code.

It is built around three priorities:

- **Accurate shooting:** it picks an exposed body part, leads moving targets, and fires
  only when the torso will really be on target when the shot leaves.
- **Laser first:** it remembers lasers it has seen, goes for them before optional fights,
  swaps out weaker weapons, and releases full charges once locked on.
- **Controlled aggression:** it chases and prefers wounded targets. When badly hurt or
  outnumbered at low health it runs to cover and holds fire so that it can regenerate.

## Files

| File | Purpose |
| --- | --- |
| `bot.py` | Strategy, movement, weapons and loot |
| `gunnery.py` | Target tracking and aiming maths |
| `arena.py` | Map, line of fire and routing |
| `mechfight.py` | The organisers' official Python kit, unmodified |

## Run

```sh
docker build -t professor .
docker run --rm -i professor
```

## Credits

- The game protocol and `mechfight.py` are by the Buildathon organisers. `mechfight.py`
  is not covered by this repository's license.
- The bot was written from the protocol. Before it was written, the Apex-Go project's
  notes and small parts of its source were read, and its ideas were an inspiration.
  Those notes credit TigerTown27, giddygiddyboy and other public Buildathon teams.
  No code was copied from these projects.
- The READMEs (not the code) of azz0048/Mech-Royale, sparshagra/AIEthicsTuts and
  andrew-zhouu/builders-mech-royale-2026 were viewed during development. The latter two
  ask not to be reused by other teams, and nothing was taken from them.
