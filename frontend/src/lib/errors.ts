/** Map API errors to safe, fixed user messages (server details are codes only). */
import { CombinedGraphQLErrors, ServerError } from "@apollo/client/errors";

const MESSAGES: Record<string, string> = {
  NOT_FOUND: "Not found, or you do not have access.",
  FORBIDDEN: "Your role does not allow this action.",
  REAUTH_REQUIRED: "Please confirm your password to continue.",
  CONFLICT: "That conflicts with the current state.",
  VALIDATION_FAILED: "Some fields are invalid.",
  RATE_LIMITED: "Too many requests. Please wait and try again.",
  SERVICE_UNAVAILABLE: "A dependency is unavailable. Please retry shortly.",
  TIMEOUT: "The request timed out. Please retry.",
  UNAUTHENTICATED: "Your session has ended. Please sign in again.",
  INVALID_CREDENTIAL: "Email or password is incorrect.",
  QUERY_TOO_COMPLEX: "That request is too large.",
};

export function errorCodes(error: unknown): string[] {
  if (CombinedGraphQLErrors.is(error)) return error.errors.map((e) => String(e.extensions?.code ?? "UNKNOWN"));
  if (ServerError.is(error)) {
    try {
      const body = JSON.parse(error.bodyText) as { errors?: { extensions?: { code?: string } }[] };
      return (body.errors ?? []).map((e) => e.extensions?.code ?? "UNKNOWN");
    } catch {
      return ["UNKNOWN"];
    }
  }
  return error ? ["UNKNOWN"] : [];
}

export function errorDetails(error: unknown): { field?: string; code: string }[] {
  if (!CombinedGraphQLErrors.is(error)) return [];
  return error.errors.flatMap((e) => (e.extensions?.details as { field?: string; code: string }[] | undefined) ?? []);
}

export function messageFor(code: string): string {
  return MESSAGES[code] ?? "Something went wrong. Please retry.";
}

export function describeError(error: unknown): string {
  const codes = errorCodes(error);
  const details = errorDetails(error);
  const base = messageFor(codes[0] ?? "UNKNOWN");
  return details.length ? `${base} (${details.map((d) => [d.field, d.code].filter(Boolean).join(": ")).join(", ")})` : base;
}
