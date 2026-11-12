# infrastructure

how the platform itself is deployed.

not built yet. `crittertown/selfhost.md` and `docker-compose.yml` are the answer today for one
box; this root is for what comes after that, when the deployment stops being a compose file.
nothing here belongs to an agent: an agent vm never copies this directory, which is what
`crittertown/tests/test_boundaries.py` enforces for every platform root.
