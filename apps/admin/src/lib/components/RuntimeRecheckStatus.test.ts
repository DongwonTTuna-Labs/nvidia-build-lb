import { describe, expect, test } from "bun:test";
import { compile } from "svelte/compiler";
import { render } from "svelte/server";
import { leaseRecheckMessage } from "../operations";

async function renderStatus(message: string): Promise<string> {
  const source = await Bun.file(new URL("./RuntimeRecheckStatus.svelte", import.meta.url)).text();
  const compiled = compile(source, {
    filename: "RuntimeRecheckStatus.svelte",
    generate: "server",
  });
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(compiled.js.code).toString("base64")}`;
  const component = (await import(moduleUrl)).default;
  return render(component, { props: { message } }).body;
}

describe("runtime recheck accessible result", () => {
  test("keeps every terminal outcome in a persistent live region", async () => {
    const outcomes = [
      leaseRecheckMessage({
        freshOverview: true,
        ownerLeaseReady: true,
        lastSuccessLabel: "방금",
      }),
      leaseRecheckMessage({
        freshOverview: true,
        ownerLeaseReady: false,
        lastSuccessLabel: "방금",
      }),
      leaseRecheckMessage({
        freshOverview: false,
        ownerLeaseReady: false,
        lastSuccessLabel: "2026. 7. 21. 09:00",
      }),
      leaseRecheckMessage({
        freshOverview: false,
        ownerLeaseReady: true,
        lastSuccessLabel: "2026. 7. 21. 09:01",
      }),
    ];

    for (const outcome of outcomes) {
      const body = await renderStatus(outcome);
      expect(body).toContain('role="status"');
      expect(body).toContain('aria-live="polite"');
      expect(body).toContain('aria-atomic="true"');
      expect(body).toContain(outcome);
    }
  });
});
