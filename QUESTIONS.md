# Questions for the owner

The build loop adds questions here instead of stopping. Answer by writing on
the `**Answer:**` line, in any form. You can answer while the loop runs. On
each pass, the loop:
- picks up answered questions
- records the decision in CLAUDE.md
- unblocks the TODO item
- moves the question to "Resolved"

Each question has a recommendation. Answering "ok" accepts it.

## Open

_None right now._

## Resolved

### Q1: How does a user choose the object for a presigned share link?

- **Blocks:** TODO "Presigned share links" (Phase 2)
- **Context:** there is no file browser (decision 3), but a share link points
  to one object.
- **Options:**
  1. A text field for the object key, with prefix autocomplete from a
     read-only listing
  2. A read-only tree listing (browse only, no upload or delete) with a
     "Share" action per object
  3. A key field only, with no listing
- **Recommendation:** 1. It is fast for users who know their paths, and it
  avoids building something close to a file browser.

**Answer:** 1)

**Decision:** option 1, a key field with prefix autocomplete. Recorded in CLAUDE.md.
