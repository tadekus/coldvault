# AWS identities for ColdVault

Day to day, nothing ColdVault runs can delete from your archive. Deleting
needs a separate AWS **role** that ColdVault unlocks only for a deletion
you have confirmed, with a code from your authenticator app, and only for a
few minutes.

| Identity | Used by | Can | Can never |
|---|---|---|---|
| `coldvault-master` (IAM user) | the master, every day | upload, restore, download, list, audit; *ask* to unlock the delete role | delete — an explicit Deny, so a broader policy attached later can't change that |
| `coldvault-agent` (IAM user, one per machine is fine) | a second machine (agent) | upload, download | delete, restore, list |
| `ColdVaultDelete` (IAM role) | the master, only while a deletion runs | delete objects in your buckets | anything else |

**Why a role and not a second user with delete rights?** A second user's
access keys would sit in `.env` all the time. Anyone who got hold of that
file (a stolen backup, a compromised machine) could delete your archive, and
nothing would stop them. A role has no keys of its own. Your everyday user can
only *request* temporary role credentials, and the role's trust policy grants
that request **only with a fresh MFA code**. So a leaked `.env` still can't
delete anything, and every deletion shows up in AWS CloudTrail as an
`AssumeRole` with MFA, followed by the deletes.

## What happens when you delete

1. You preview, confirm, and type the bucket name, as before.
2. ColdVault asks for the 6-digit code from your authenticator app.
3. It calls `sts:AssumeRole` with that code and gets credentials that expire
   after 15 minutes. It uses them for this one deletion and then discards them.
   They never touch disk or the log.
4. A wrong or reused code means AWS refuses the role, and nothing is deleted.

The **Test delete role** button on the Retention tab runs steps 2 and 3 plus
a harmless probe: a delete of a random key that never existed. That confirms
the whole setup works without removing anything.

## Setting it up

You need an **administrator** profile on your own computer for this, never
ColdVault's keys. The commands below use `--profile admin`.

### 1. Write the policies

```bash
cd deploy/iam
./make-policies.sh 123456789012 my-archive-bucket second-bucket   # your account number + your buckets
```

This writes `out/master-user.json`, `out/agent-user.json`, `out/delete-role.json`
and `out/delete-role-trust.json`. Read them first. They're short.

**Already have a user on the master?** Keep it, and generate with its name:
`MASTER_USER=your-user-name ./make-policies.sh …`. Then skip `create-user` and
`create-access-key` in step 2; `put-user-policy` sets its permissions.
If the user has *managed* policies attached (e.g. `AmazonS3FullAccess`),
detach them. The explicit Deny would still block deletes, but the user should
have only what ColdVault needs.

### 2. The everyday user on the master

```bash
aws iam create-user --user-name coldvault-master --profile admin
aws iam put-user-policy --user-name coldvault-master --policy-name coldvault \
    --policy-document file://out/master-user.json --profile admin
aws iam create-access-key --user-name coldvault-master --profile admin
```

Put the access key into the master's `.env` (`AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`).

### 3. An MFA device for that user

Easiest in the AWS console: **IAM → Users → coldvault-master → Security
credentials → Assign MFA device → Authenticator app**, then scan the QR code
with your phone. Note the device's ARN, e.g.
`arn:aws:iam::123456789012:mfa/coldvault-master`.

### 4. The delete role

```bash
aws iam create-role --role-name ColdVaultDelete \
    --assume-role-policy-document file://out/delete-role-trust.json --profile admin
aws iam put-role-policy --role-name ColdVaultDelete --policy-name delete \
    --policy-document file://out/delete-role.json --profile admin
```

(Step 2 must come first: AWS checks that the user named in the trust policy
exists.)

### 5. Tell ColdVault (the master's `.env`)

```
COLDVAULT_DELETE_ROLE_ARN=arn:aws:iam::123456789012:role/ColdVaultDelete
COLDVAULT_DELETE_MFA_SERIAL=arn:aws:iam::123456789012:mfa/coldvault-master
```

Apply with `docker compose up -d`. In **Retention**, press **Re-check**: every
bucket should show 🔒 *append-only* for the everyday user. Then press **Test
delete role**, enter a code, and it should report that the role can delete,
with nothing removed.

### 6. A user for each agent

```bash
aws iam create-user --user-name coldvault-agent --profile admin
aws iam put-user-policy --user-name coldvault-agent --policy-name coldvault \
    --policy-document file://out/agent-user.json --profile admin
aws iam create-access-key --user-name coldvault-agent --profile admin
```

These keys go into the agent's `.env`. An agent never deletes and never needs
the role. With several agents you can create one user per machine (same
policy) so you can revoke one without touching the others.

## Adding a bucket later

Run `make-policies.sh` again with the full list of buckets and repeat the
`put-user-policy` / `put-role-policy` commands. They replace the previous
version.

## Without MFA

`NO_MFA=1 ./make-policies.sh …` writes a trust policy without the MFA
condition. Leave `COLDVAULT_DELETE_MFA_SERIAL` empty and ColdVault unlocks the
role with your typed confirmation alone. That still keeps delete rights out of
day-to-day use, but anyone holding the master's keys could unlock the role
too, so MFA is strongly recommended.
