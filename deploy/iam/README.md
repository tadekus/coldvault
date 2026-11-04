# AWS identities for ColdVault

The full guide — which identities, why a delete *role* instead of a delete user,
console and CLI steps, testing and troubleshooting — is in the main README:
[AWS users and permissions](../../README.md#aws-users-and-permissions).

This folder holds the policy generator:

```bash
./make-policies.sh 123456789012 my-archive-bucket second-bucket    # your account number + all your buckets
MASTER_USER=my-existing-user ./make-policies.sh …                     # keep an existing everyday user
```

It writes `out/master-user.json`, `out/delete-role-trust.json`,
`out/delete-role.json` and `out/agent-user.json` and makes **no AWS calls**.
`out/` is git-ignored. Other options: `AGENT_USER=…`, `DELETE_ROLE=…`, and
`NO_MFA=1` for a trust policy without the MFA condition (not recommended).
