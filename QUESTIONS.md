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

### Q2: Budget numbers (index/worktag) on buckets: format and handling

- **Blocks:** TODO "User profile" and "Bucket list ... Create bucket" (Phase 2)
- **Decided by the owner:** every new bucket needs a budget number
  (index/worktag). Users can save a default in their profile, and the
  create-bucket form prefills it.
- **Open points**, with a recommendation for each:
  1. **Format and validation:** what does a valid budget number look like
     (pattern or example)? Is there a system Bucketeer could check it
     against? *Recommendation: an admin-editable regex on the admin page, plus
     an optional example shown as the hint. No live lookup for now.*
  2. **Where it is stored:** *Recommendation: in SQLite, and also as a
     Bucketeer-managed bucket tag (`bucketeer-budget=<value>`) on RGW, so
     that S3 tooling can see it and it survives if the database is lost. The
     tagging editor under Advanced will protect `bucketeer-*` tags.*
  3. **Changing it later:** can the owner change a bucket's budget number
     after creation? *Recommendation: yes, with an audit-log entry recording
     the old and new values.*
  4. **Existing buckets** that have no budget number: *Recommendation: flag
     them in the bucket list and on the admin page, and ask the owner to fill
     one in. Don't block their use.*
  5. **Reporting:** do admins need a chargeback export (bucket, owner,
     budget number, usage)? *Recommendation: yes, a CSV export on the admin
     page.*

**Answer:**

### Q3: What should users see when an account's login limit is reached?

- **Blocks:** nothing. Current behavior stays until you answer.
- **Context:** after 5 failed logins on one AD account within 15 minutes,
  Bucketeer stops trying passwords for that account, to protect it from AD
  lockout. Today it answers "Too many attempts, try again later" (HTTP 429).
  An attacker who has already spent 5 real attempts on an account can use
  that message to tell that the account exists. That costs 5 attempts per
  account, so it can't be used to check many accounts cheaply.
- **Options:**
  1. Keep "Too many attempts". This is clearer for real users who mistype
     their password.
  2. Answer "Invalid username or password" for the first few blocked
     attempts, then "Too many attempts", the same way for real and unknown
     accounts. This closes the leak, but a user who types the right password
     while blocked is told it's wrong.
- **Recommendation:** 1. The leak is expensive to use, and option 2
  confuses legitimate users.

**Answer:**

### Q4: How are AD accounts mapped to RGW users, and created on first login?

- **Blocks:** TODO "RGW user resolution", and the items that need a
  resolved uid: "Credentials API and screen" and "Key management"
- **Context:** CLAUDE.md says the RGW uid is the AD `sAMAccountName`, with
  admin mappings for exceptions, and that the first login auto-creates the
  RGW user with the default quota from the admin page. Three details are
  open:
  1. **Case.** AD names are case-insensitive (`JDoe` and `jdoe` are the same
     account), but RGW uids are case-sensitive. Existing RGW users may use
     either case.
  2. **No default quota set yet.** What happens on first login before an
     admin has set the default quota on the admin page?
  3. **Email collision.** RGW rejects an email address that another RGW user
     already has, for example a legacy account with the same address, so
     auto-create would fail.
- **Also revisit once answered:** the admin page finds a user's bucket keys by
  treating their RGW uid as their AD username (`bucketeer/admin.py`). Adjust
  that to the chosen mapping.
- **Recommendations:**
  1. New uids are the lowercased `sAMAccountName`. At login, if an existing
     RGW user matches case-insensitively (checked against croit's uid list),
     use that one and write an audit entry. Admin mappings always win.
  2. Create the user **without** a quota, show a "default quota not set"
     warning on the admin page, and audit the creation, so admins can tidy
     up later. The alternative is to refuse auto-create until a default is
     set (safer on cost, but it blocks users on day one).
  3. Retry the creation without an email, and write an audit warning naming
     the conflicting address, so the user still gets access.

**Answer:**

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
