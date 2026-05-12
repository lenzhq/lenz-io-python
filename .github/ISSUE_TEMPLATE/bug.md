---
name: Bug report
about: Report a defect in the SDK
title: '[bug] '
labels: bug
---

**Description**
What happened? What did you expect?

**Reproducer**
A minimal example, ideally runnable. Sanitize any API keys.

```python
from lenz_io import Lenz
client = Lenz(api_key="…")
…
```

**Environment**
- SDK version (`pip show lenz-io`): 
- Python version (`python --version`): 
- OS: 

**Request ID(s)**
If you saw an error, paste the `X-Request-ID` value. Helps us look up the exact server-side log.
