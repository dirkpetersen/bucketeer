// Builds the ~/.aws/config and ~/.aws/credentials snippets in the browser from
// GET /api/credentials, so a secret never sits in any server response except the
// audited reveal. The server validates profile, region and endpoint at startup;
// the checks here only keep an odd value from ever adding INI lines.
const SAFE_VALUE = /^[\x21-\x7e]+$/; // printable ASCII, no whitespace or newlines
export const isSafe = (v) => typeof v === "string" && SAFE_VALUE.test(v);

const section = (profile) => (profile === "default" ? "[default]" : `[profile ${profile}]`);

/** Text for ~/.aws/config, or null when the endpoint is unknown. */
export function configSnippet({ profile, region, endpointUrl, addressingStyle }) {
  if (!endpointUrl || ![profile, region, endpointUrl, addressingStyle].every(isSafe)) return null;
  return [
    "# Merge this into ~/.aws/config (do not replace the file).",
    section(profile),
    `region = ${region}`,
    `endpoint_url = ${endpointUrl}`,
    "# Newer AWS CLI/SDKs add CRC checksums that many Ceph RGW versions reject.",
    "request_checksum_calculation = when_required",
    "response_checksum_validation = when_required",
    "s3 =",
    `  addressing_style = ${addressingStyle}`,
    "",
  ].join("\n");
}

export const SECRET_PLACEHOLDER = "<reveal the secret to fill this in>";

/** Text for ~/.aws/credentials; the secret is a placeholder until it is revealed. */
export function credentialsSnippet({ profile }, accessKey, secretKey) {
  if (![profile, accessKey].every(isSafe) || (secretKey && !isSafe(secretKey))) return null;
  return [
    "# Merge this into ~/.aws/credentials (do not replace the file).",
    `[${profile}]`,
    `aws_access_key_id = ${accessKey}`,
    `aws_secret_access_key = ${secretKey || SECRET_PLACEHOLDER}`,
    "",
  ].join("\n");
}

export const testCommand = (profile) => (profile === "default" ? "aws s3 ls" : `aws --profile ${profile} s3 ls`);
