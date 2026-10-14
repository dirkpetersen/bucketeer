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

### Q11: Group shares: which groups, and how big?

- **Blocks:** TODO "Group shares". Sharing with individual people is being
  built now.
- **Context:** this is the unanswered part 3 of Q8. A bucket policy can't name
  an AD group, so a group share is turned into one share per member at the
  moment you share. People who join the group later don't get access until
  the group is shared again.
- **Options:**
  1. Any AD group, found by name, with a cap of 50 members; larger groups
     are refused with a message. The share list shows the members that were
     added, and sharing the group again adds new members.
  2. Only groups an admin has put on an allowlist in Settings, with the same
     cap.
  3. No group shares; people share with individuals only.
- **Recommendation:** 1.

**Answer:**

### Q12: Public buckets and self-lockout in the raw policy editor

- **Blocks:** nothing. The editor currently accepts both cases below, with
  the usual warning for Bucketeer-managed statements.
- **Context:** with Advanced on, users can edit their bucket's raw policy.
  1. **Public buckets:** a statement with `"Principal": "*"` makes a bucket
     readable (or writable) by anyone, and RGW accepts it.
  2. **Locking yourself out:** an explicit Deny on `s3:*` (or on the policy
     actions) for everyone also stops the owner; only an admin can then fix
     it in RGW, and Bucketeer's sharing, keys and lifecycle on that bucket
     start failing.
  Important: users have their own S3 keys, so they can set any policy with
  the AWS CLI regardless of what Bucketeer allows. Bucketeer can warn and
  guide; real enforcement would need RGW/cluster settings.
- **Options:**
  1. Allow both, with a clear warning and an extra confirmation in the
     editor, and audit them so admins can see them.
  2. Refuse them in the editor (users could still do it with the CLI).
  3. Also add an admin-page list of buckets with public statements.
- **Recommendation:** 1 plus 3: warn, confirm and audit in the editor, and
  give admins a "public buckets" list, since refusing in the UI alone gives a
  false sense of safety.

**Answer:**

### Q13: May lifecycle rules move data to another storage class?

- **Blocks:** TODO "Lifecycle transitions". The rest of the full lifecycle
  editor is being built now without them.
- **Context:** a lifecycle "transition" moves objects older than N days to a
  different storage class, typically a cheaper, slower tier such as an
  erasure-coded or HDD pool. Q6 decided users never pick a storage class for
  new data. Moving data later is a related but separate choice, and it can
  affect cost and performance on the cluster.
- **Options:**
  1. No transitions in Bucketeer; admins handle tiering.
  2. Transitions allowed, but only to storage classes an admin enables for
     this purpose on the Settings tab (default: none).
  3. Transitions to any storage class of the bucket's placement.
- **Recommendation:** 2. Admins decide which tiers are open for automatic
  moves, and users can use them without seeing the rest.

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

**Answer:** yes to all, but 4. only admins can create a buckets if there is no tag

**Decision:** recommendations 1, 2, 3 and 5 accepted. For 4: users must always enter a budget number, and only admins can create a bucket without one. Existing buckets without one are flagged but not blocked. Recorded in CLAUDE.md.

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

**Answer:** 1 but we want 10 attempts

**Decision:** option 1, keep "Too many attempts", but allow 10 attempts instead of 5. Added as a TODO item.

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

**Answer:** 1. yes 2. the default quota is hardcoded 1TB and can be changed. 3. We want to allow email address login but what actually happens is that if an email address is used this may or may not be the User principal name it may be the email So then we have to search Active Directory for that email and then look up the associated user SAM account name We would never create an account based on an email address

**Decision:** 1 accepted. 2: the default quota defaults to 1 TiB and admins can change it. 3: the answer clarified email login. An email address is resolved through AD to the sAMAccountName, and a uid is never derived from an email address. The original sub-question (RGW rejecting a duplicate email on create) moves to Q5. Recorded in CLAUDE.md.

### Q5: What should happen when RGW rejects a new user's email address?

- **Blocks:** nothing. The current safe behavior is described below.
- **Context:** this is not about logging in with an email address; that is
  settled in Q4. When Bucketeer auto-creates the RGW user on first login, it
  passes the user's AD email to croit. RGW refuses an email that another RGW
  user already has, for example an old or shared S3 account registered with
  the same address, so the create fails.
- **Current behavior (safe placeholder):** the login succeeds, but the user
  sees "Your S3 account couldn't be created, contact your administrator", and
  an audit entry names the conflict. No account is created.
- **Options:**
  1. Keep it that way: an admin resolves the conflict, for example by adding
     a uid mapping to the existing RGW user.
  2. Create the RGW user without an email and audit a warning, so the user
     gets access immediately.
- **Recommendation:** 2. The email on the RGW user is informational, and
  blocking the user helps nobody.

**Answer:** OK , But only if Kroid actually requires an email if Kroid only requires a user then emails should never be passed there instead we should look up the username by the email address and then pass that to But of course if Croyd requires username and email then we should provide both The username is also always a valid email

**Decision:** croit's `NewUserRequest` requires only `uid` and `name` (email is optional), so Bucketeer never sends an email address when creating an RGW user. The uid always comes from the sAMAccountName; an email login is resolved through AD first. If croit ever requires an email, pass both. Recorded in CLAUDE.md; added as a TODO item.

### Q6: Storage classes and notification broker logins

- **Blocks:** TODO "Bucket notifications" (Phase 4), for part 2 only.
- **Context:** the admin Settings tab is being built now.
  1. **Storage class:** in S3 a storage class is chosen per object at upload.
     Bucketeer has no upload, so users only pick a placement for a new bucket,
     and storage classes are shown read-only. Should users ever choose a
     storage class, for example as a default?
  2. **Broker logins:** bucket notifications can go to HTTP(S), AMQP or Kafka
     endpoints on the admin allowlist. Do your AMQP or Kafka brokers need a
     username and password? If they do, Bucketeer would store them write-only:
     saved, never shown again or sent to the browser.
- **Recommendations:** 1. No, placement only. 2. Only if your brokers need
  them; otherwise leave credentials out of v1.

**Answer:** OK

**Decision:** recommendations accepted. Users choose a placement only, never a storage class, and there are no broker credentials in v1. Recorded in CLAUDE.md; bucket notifications unblocked.

### Q7: Should versioning be on by default for new buckets?

- **Blocks:** nothing. New buckets currently default to versioning **off**,
  as plain S3 does.
- **Context:** versioning keeps every overwritten or deleted object version,
  which protects against accidents. But old versions count toward the quota,
  and users must add lifecycle rules (expire noncurrent versions) to clean
  them up. The create dialog has a checkbox either way.
- **Options:**
  1. Default off, as now.
  2. Default on, with the simple "expire noncurrent versions after N days"
     lifecycle rule suggested once that feature exists.
- **Recommendation:** 2, on by default. Accidental deletes are the most
  common data-loss case, and the quota cost is visible in the bucket list.

**Answer:** On by default and expire non current versins after 30 days

**Decision:** versioning is on by default for new buckets, and a new versioned bucket gets the lifecycle rule "expire noncurrent versions after 30 days". Recorded in CLAUDE.md; added as a TODO item.

### Q8: Sharing a bucket: who can receive a share, and how?

- **Blocks:** TODO "Sharing"
- **Context:** sharing adds a bucket-policy statement that grants another
  person's S3 user read-only or read-write access. To do that, Bucketeer has
  to find the person in AD and know their S3 user.
  1. **Recipient has no S3 account yet:** they have never logged in to
     Bucketeer, so they have no RGW user to name in the policy.
  2. **Recipient outside the required AD group:** can you share with someone
     who isn't allowed to log in to Bucketeer?
  3. **Group shares:** CLAUDE.md says a group is expanded to its members'
     S3 users when you share, so later joiners don't get access
     automatically. Which groups can be picked, and is there a size limit?
- **Recommendations:**
  1. Create their S3 account on the spot, with the default quota, the same
     as their first login would. The share then works immediately and they
     just need to log in to get keys.
  2. No. Only members of `LDAP_REQUIRED_GROUP_DN` can receive shares, so
     Bucketeer never creates accounts for people who couldn't use them.
  3. Any AD group, found by name, with a cap of 50 members; larger groups
     are refused with a message. The share list shows which members were
     added, and re-sharing the group adds new members.

**Answer:** 2. if LDAP_REQUIRED_GROUP_DN is set, 1. if it is not set. if not set everyone can login

**Decision:** recipients are exactly the people who could log in: members of `LDAP_REQUIRED_GROUP_DN` when it is set (recommendation 2), anyone in AD when it is empty (recommendation 1); a recipient without an S3 account gets one created on the spot. Group shares (part 3) were not answered and moved to Q11. Recorded in CLAUDE.md; individual sharing unblocked.

### Q9: How many bucket keys can one user create?

- **Blocks:** nothing. The default below is used until you answer.
- **Context:** a bucket key is a separate S3 user that can only reach one
  bucket, read-only or read-write. Each key adds an RGW user to the cluster.
- **Default being built:** at most **10 live bucket keys per user** (revoked
  keys don't count), and at most 5 per bucket.
- **Recommendation:** keep 10 per user and 5 per bucket. Admins can always
  see and revoke them.

**Answer:** OK

**Decision:** keep 10 live bucket keys per user and 5 per bucket. Recorded in CLAUDE.md.
