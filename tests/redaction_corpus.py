"""Ordinary prompts and code, as a Claude Code user pastes them, for the redactor (R9k).

Not a test module. ``tests/test_redaction_keeps_ordinary_text.py`` holds what ``redact`` makes of
each at ``standard``. Each was also run through 0.9.2's redactor, and every difference is named
there: a credential 0.9.2 missed, or a header with no key after it. Credential-shaped values are
assembled, not written out, as elsewhere in the suite: a scanner reads a realistic shape in a file
as a live key.
"""

from __future__ import annotations

HEADER = "-----BEGIN RSA PRIVATE KEY-----"
OPENSSH = "-----BEGIN OPENSSH PRIVATE KEY-----"
GHP = "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
GOOGLE_ACCESS = "ya29" + ".a0AfB_byC1d2E3f4G5h6I7j8K9l0M1n2O3p4Q5r6S7t8U9v0"
NPM = "npm" + "_" + "abcdefghijklmnopqrstuvwxyz0123456789"
WHSEC = "whsec" + "_" + "abc123def456ghi789"
OPENSSH_LINE = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAABlwAAAAdzc2gtcn"
NPM_LIKE = "npm" + "_" + "Zq8vR2mXk4Lp9Tn3Ab1Cd2Ef3Gh4Ij5Kl6Mn7"
HF = "hf" + "_" + "Zq8vR2mXk4Lp9Tn3Ab1Cd2Ef3Gh4Ij5"

#: (where it comes from, the text). Grouped as the brief asked: JSON configs, API docs, shell
#: sessions, stack traces, and code that names token, key and secret variables.
CORPUS: tuple[tuple[str, str], ...] = (
    # --- JSON and YAML configs ---------------------------------------------------------------
    ("openai body", '{"model": "gpt-4o", "max_tokens": "8192", "temperature": 0.2}'),
    (
        "anthropic body",
        '{"model": "claude-opus-4", "max_tokens": 4096, '
        '"messages": [{"role": "user", "content": "hi"}]}',
    ),
    (
        "package.json",
        '{"name": "web", "scripts": {"build": "tsc -p .", "test": "vitest"}, '
        '"dependencies": {"jsonwebtoken": "^9.0.2", "keytar": "^7.9.0"}}',
    ),
    (
        "oauth discovery",
        '{"issuer": "https://accounts.example.com", '
        '"token_endpoint": "https://oauth2.example.com/token", '
        '"authorization_endpoint": "https://accounts.example.com/o/oauth2/v2/auth", '
        '"token_endpoint_auth_methods_supported": ["client_secret_post"]}',
    ),
    (
        "oauth token response",
        f'{{"access_token": "{GOOGLE_ACCESS}", "token_type": "Bearer", '
        '"expires_in": 3599, "scope": "openid email"}',
    ),
    (
        "k8s secret manifest",
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db-secret\ntype: Opaque\n"
        'stringData:\n  password: "hunter2hunter2"',
    ),
    (
        "docker config.json",
        '{"auths": {"https://index.docker.io/v1/": {"auth": "dXNlcjpwYXNzd29yZA=="}}}',
    ),
    (
        "github actions",
        "env:\n  GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n  NODE_ENV: production",
    ),
    (
        "vscode settings",
        '{"editor.tabSize": 2, "github.copilot.advanced": {"authProvider": "github"}, '
        '"git.autofetch": true}',
    ),
    (
        "iam policy",
        '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
        '"Action": "secretsmanager:GetSecretValue", "Resource": "*"}]}',
    ),
    ("terraform", 'variable "db_password" {\n  type      = string\n  sensitive = true\n}'),
    (
        "cloudformation",
        '{"Parameters": {"ApiKeyName": {"Type": "String", "Default": "my-key"}}}',
    ),
    (
        "usage log",
        '{"usage": {"input_tokens": 1234, "output_tokens": 567, "cache_read_input_tokens": 0}}',
    ),
    ("tokenizer config", '{"tokenizer": "cl100k_base", "max_tokens": "4096", "stop": ["\\n"]}'),
    ("i18n", '{"key": "button.save", "defaultMessage": "Save"}'),
    (
        "jwt config",
        '{"jwt": {"secret_name": "prod/jwt", "token_ttl": "3600", "algorithm": "RS256"}}',
    ),
    (
        "password policy",
        '{"password_policy": {"min_length": 12}, "password_hint": "your first pet"}',
    ),
    ("stripe webhook", f'{{"endpoint_secret": "{WHSEC}"}}'),
    (
        "env dump",
        '{"PWD": "/Users/me/project", "OLDPWD": "/Users/me", "SHELL": "/bin/zsh", '
        '"TERM": "xterm-256color"}',
    ),
    (
        "api config",
        '{"auth_token_url": "https://login.example.com/token", "token_type": "Bearer", '
        '"key_count": "3", "num_keys": "4", "api_key_header": "X-API-Key"}',
    ),
    # --- API docs ----------------------------------------------------------------------------
    (
        "docs bearer",
        "Send the token in the Authorization header: `Authorization: Bearer <token>`. Tokens "
        "expire after an hour; call /oauth/token with grant_type=refresh_token for a new one.",
    ),
    (
        "docs fields",
        'The response includes `access_token`, `token_type` (always "Bearer") and `expires_in`.',
    ),
    (
        "docs env",
        "Set `API_KEY` in your environment, e.g. `export API_KEY=your-key-here`.",
    ),
    (
        "docs query key",
        "Pass the key as a query parameter: GET /v1/search?q=cats&key=YOUR_API_KEY",
    ),
    (
        "docs schema",
        '{"properties": {"password": {"type": "string", "minLength": 12}, '
        '"api_key": {"type": "string", "pattern": "^sk-"}}}',
    ),
    (
        "openapi",
        '"securitySchemes": {"ApiKeyAuth": {"type": "apiKey", "in": "header", '
        '"name": "X-API-Key"}}',
    ),
    (
        "markdown table",
        '| Field | Description |\n|---|---|\n| `token_type` | always "Bearer" |\n'
        "| `access_token` | the token |",
    ),
    # --- shell sessions ----------------------------------------------------------------------
    (
        "git push",
        "$ git push origin main\nTo github.com:org/repo.git\n   5a1bac4..8e40ad9  main -> main",
    ),
    ("env grep", f"$ env | grep -i token\nGITHUB_TOKEN={GHP}\nNO_TOKENS=1"),
    (
        "kubectl",
        "$ kubectl get secrets\nNAME                  TYPE                                  "
        "DATA   AGE\ndefault-token-x7k2p   kubernetes.io/service-account-token   3      12d",
    ),
    (
        "ssh-keygen",
        '$ ssh-keygen -t ed25519 -C "me@example.com"\nGenerating public/private ed25519 key '
        "pair.\nEnter file in which to save the key (/home/me/.ssh/id_ed25519):",
    ),
    (
        "head of a key",
        f"$ head -3 ~/.ssh/id_rsa\n{OPENSSH}\n{OPENSSH_LINE}\n{OPENSSH_LINE[:40]}\n"
        "$ why is this key rejected?",
    ),
    (
        "grep for headers",
        f"$ grep -rn 'BEGIN RSA PRIVATE KEY' .\n./docs/keys.md:3:{HEADER}\n"
        f"./scripts/check.sh:12:if grep -q '{HEADER}' \"$f\"; then",
    ),
    (
        "npm test",
        "$ npm test\n\n> web@1.0.0 test\n> vitest run\n\n ✓ src/auth/token.test.ts (12 tests) "
        "34ms\n\n Test Files  1 passed (1)",
    ),
    ("psql url", '$ psql "postgresql://app:s3cr3tP4ss@db.internal:5432/app"'),
    ("npmrc", f"Here is my .npmrc: //registry.npmjs.org/:_authToken={NPM}"),
    # --- stack traces ------------------------------------------------------------------------
    (
        "python traceback",
        'Traceback (most recent call last):\n  File "/srv/app/auth/tokens.py", line 42, in '
        "refresh\n    resp = client.fetch(url)\nKeyError: 'access_token'",
    ),
    (
        "node trace",
        "TypeError: Cannot read properties of undefined (reading 'token')\n    at verifyToken "
        "(/app/src/middleware/auth.js:17:23)\n    at Layer.handle [as handle_request] "
        "(/app/node_modules/express/lib/router/layer.js:95:5)",
    ),
    (
        "java trace",
        "javax.crypto.BadPaddingException: Given final block not properly padded\n\tat "
        "com.example.SecretStore.decrypt(SecretStore.java:88)",
    ),
    (
        "go panic",
        "panic: runtime error: invalid memory address\ngoroutine 1 [running]:\n"
        "main.loadAPIKey(...)\n\t/srv/app/main.go:21",
    ),
    # --- code that names tokens, keys and secrets --------------------------------------------
    (
        "js oauth call",
        'const { data } = await axios.post(url, { "grant_type": "client_credentials", '
        '"client_id": id, "client_secret": process.env.CLIENT_SECRET });',
    ),
    (
        "go struct tags",
        'type Config struct {\n\tAPIKey    string `json:"api_key"`\n\tMaxTokens int    '
        '`json:"max_tokens"`\n}',
    ),
    ("python headers", 'headers = {"Authorization": f"Bearer {token}", "x-api-key": api_key}'),
    ("python fixture", 'FAKE = {"api_key": "test-key-not-real", "token": "dummy"}'),
    ("jsx", '<Item key="row-1" token="abc" />'),
    ("env example", "DATABASE_URL=postgres://localhost/app\nSTRIPE_PUBLISHABLE_KEY=pk_test_123"),
    (
        "prose",
        "Our tokenizer splits the prompt into tokens; the secret is batching. Keys rotate "
        "monthly and passwords never leave the vault.",
    ),
    ("prose quoted", 'The "token" and "secret" fields are required; "password": optional.'),
    ("log line", "2026-11-06T10:00:00Z INFO auth: token refreshed for user=42 (expires_in=3600)"),
    ("json pair", '{"key": "value", "secret": "s3cr3t-v4lue"}'),
    # --- unquoted names, read as 0.9.2 read them (and taken as it took them) --------------------
    (
        "python sdk call",
        "def ask(client, prompt, *, max_tokens=4096):\n"
        "    return client.messages.create(max_tokens=max_tokens, messages=prompt)",
    ),
    (
        "ts interface",
        'interface TokenResponse {\n  access_token: string;\n  token_type: "Bearer";\n'
        "  expires_in: number;\n}",
    ),
    ("sql", "SELECT id FROM users WHERE reset_token = 'abc123def456';"),
    # --- reopen 1 (tester-r9i, seq 11384): what the first cut missed or took ----------------
    (
        "aws cli pagination",
        '{"Parameters": [{"Name": "/app/db-host", "Type": "String"}], '
        '"NextToken": "eyJDb250aW51YXRpb25Ub2tlbiI6IG51bGx9"}',
    ),
    ("google pagination", '{"items": [], "nextPageToken": "CAoQAA"}'),
    (
        "package specs",
        '{"dependencies": {"next-auth": "beta", "better-auth": '
        '"github:better-auth/better-auth#main", "@octokit/auth-token": "catalog:"}}',
    ),
    (
        "psql env",
        'subprocess.run(["psql", url], env={**os.environ, "PGPASSWORD": "hunter2hunter2"})',
    ),
    ("launch.json env", '{"env": {"PGPASSWORD": "postgres123", "PGUSER": "postgres"}}'),
    ("npm token by name", f'{{"mytoken": "{NPM_LIKE}", "hftoken": "{HF}"}}'),
    (
        "secrets manager binary",
        '{"ARN": "arn:aws:secretsmanager:eu-west-1:123456789012:secret:db-AbCdEf", '
        '"SecretBinary": "eyJwYXNzd29yZCI6ICJodW50ZXIyIn0=", "VersionStages": ["AWSCURRENT"]}',
    ),
    (
        "a long word after a header",
        f"{HEADER}\ninternationalization breaks when I paste the key",
    ),
    ("a path after a header", f"{HEADER}\n/home/me/projects/deploy/keys is where it lives"),
)
