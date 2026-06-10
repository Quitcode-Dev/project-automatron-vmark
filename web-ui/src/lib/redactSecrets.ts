/**
 * Client-side secret redaction for plan content previews.
 * Applied to generated file content before display; does not affect the stored plan.
 */

// Lines that look like VAR=value where VAR contains a secret keyword
const SECRET_ENV_LINE = /^(\s*[A-Z_]*(?:PASSWORD|SECRET|TOKEN|API_KEY|AUTH|PRIVATE)[A-Z_]*\s*=\s*)(.+)$/gim;

// Known API key prefixes (Anthropic, GitHub, GitLab, Slack, etc.)
const KNOWN_KEY_PATTERN = /\b(sk-ant-[A-Za-z0-9_-]+|ghp_[A-Za-z0-9]+|ghs_[A-Za-z0-9]+|glpat-[A-Za-z0-9_-]+|xox[bpoa]-[A-Za-z0-9_-]+)/g;

export function redactSecretContent(text: string): string {
  return text
    .replace(SECRET_ENV_LINE, "$1[REDACTED]")
    .replace(KNOWN_KEY_PATTERN, "[REDACTED]");
}
