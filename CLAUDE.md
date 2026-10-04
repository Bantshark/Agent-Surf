# Agent Surf

Build from `BUILD-BRIEF.md`. It is the authority; read it before any edit.

- Install only the three pinned packages in `requirements.txt`. Anything else is a gap to report, not install.
- No requests to live social sites (x.com, reddit.com, instagram.com, facebook.com, linkedin.com, youtube.com). Tests run on synthetic fixtures only.
- Public repo: never commit real cookies, tokens, auth headers or personal data. The scrub guard test must pass.
- Runner is read-only: navigate within the site's domain, scroll, wait, click stored selectors. Nothing else.
- Never bypass or interact with a CAPTCHA.
- Never print or log a secret value.
- Run tests with the preinstalled `pytest` after each unit.
