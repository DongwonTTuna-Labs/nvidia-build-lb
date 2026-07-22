import { describe, expect, test } from "bun:test";
import { acceptsHttpResponse, acceptsHttpStatus } from "./api";

describe("evidence-bearing HTTP status handling", () => {
  test("accepts an explicitly declared probe failure payload", () => {
    expect(acceptsHttpStatus(false, 422, [422])).toBe(true);
  });

  test("does not weaken authentication or unrelated error handling", () => {
    expect(acceptsHttpStatus(false, 401, [422])).toBe(false);
    expect(acceptsHttpStatus(false, 500, [422])).toBe(false);
    expect(acceptsHttpStatus(true, 202, [])).toBe(true);
  });

  test("requires an endpoint-specific payload contract for an accepted error status", () => {
    const evidence = { item: { status: "failed" } };
    const validator = (value: unknown) => value === evidence;
    expect(acceptsHttpResponse(false, 422, [422], evidence, validator)).toBe(true);
    expect(
      acceptsHttpResponse(false, 422, [422], { error: { code: "validation_error" } }, validator),
    ).toBe(false);
    expect(acceptsHttpResponse(false, 422, [422], evidence)).toBe(false);
  });
});
