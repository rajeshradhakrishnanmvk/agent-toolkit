---
name: Harness eval result
about: Submit a tiny-tools leaderboard result
title: "Eval result: "
labels: []
---

## Model

- Leaderboard label:
- Model id returned by the API:

## Result file

- Path: `docs/harness/results/<file>.json`
- Eval set: `tiny-tools` version 1
- Content hash:

## Checklist

- [ ] I ran **Run eval** on the live GitHub Pages harness: https://rajeshradhakrishnanmvk.github.io/agent-toolkit/harness/
- [ ] This is a live run. A `?stub=1` preview is not a result.
- [ ] The JSON has no API key, no Authorization header, and no model endpoint URL.
- [ ] The file is listed in `docs/harness/results/index.json`.
- [ ] `node docs/harness/test.js` passes, including the results validator.
