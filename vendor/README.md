# vendored code

these three packages are copies of permissively licensed upstream code, committed
here so clawcrew runs without shipping its own reimplementation of someone else's
wire contract. nothing in this directory is ours: each package keeps its upstream
`LICENSE` and a `PROVENANCE.md` naming the repository, the commit and the files.

the directory name is ours and says what the thing is for (`persistent_cloud_api`,
`ephemeral_cloud_api`, `a2a_types`); the upstream project is named inside
`PROVENANCE.md` and in `clawcrew/thirdparty.md`. every import inside a vendored package
is relative, so a rename touches no file's contents.

| package | upstream | licence | what it is |
| --- | --- | --- | --- |
| `persistent_cloud_api` | [CelestoAI/celesto](https://github.com/CelestoAI/celesto) `src/_celesto_cloud_api` | Apache-2.0 | the client generated from `openapi/cloud.json` |
| `ephemeral_cloud_api` | [e2b-dev/E2B](https://github.com/e2b-dev/E2B) `packages/python-sdk/e2b/api/client` | MIT | the client generated from `spec/openapi.yml` |
| `a2a_types` | [a2aproject/a2a-python](https://github.com/a2aproject/a2a-python) `src/a2a/compat/v0_3/types.py` + `src/a2a/_base.py` | Apache-2.0 | the pydantic models generated from the a2a json schema |

## why vendored instead of a dependency

the adapters used to re-type vendor routes and json fields by hand, written from the
published specs. that is how a client silently rots: the spec moves, the copy in our
code does not. these are the vendors' own generated clients, so a contract change
arrives as a diff in one vendored file, and the request bodies are built by the code
the vendor tests.

it also keeps the install honest: `pip install -e .` pulls `attrs` and `httpx`, and
the hosted adapters work with no extra package. command execution inside an ephemeral sandbox still needs
the upstream sdk, because commands go over envd rather than the sandbox REST api, so that
lives behind `pip install -e ".[e2b]"`.

## rules for this directory

- do not edit vendored files by hand. re-copy from upstream and record the new commit
  in that package's `PROVENANCE.md`.
- the only accepted local change is an import rewrite required by the move (see
  `a2a_types/PROVENANCE.md`); anything else means the file is not vendored code any
  more and belongs in `clawcrew/`.
- tests in `tests/test_adapters.py` and `tests/test_a2a_protocol.py` drive the real
  generated code through an `httpx.MockTransport`, so a bad re-copy fails the suite.
