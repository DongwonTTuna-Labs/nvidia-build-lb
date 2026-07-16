class AdminRequestTimeoutError extends Error {}

function parseResponseProblem(path, status, source) {
  if (path === "/dashboard" && status === 404) {
    return {code: "incompatible_service", message: "this service does not provide the required dashboard contract", request_id: "unavailable"};
  }
  const payload = parseJson(source);
  const kind = status === 422 ? "validationError" : "error";
  return parseAdminDto(kind, payload).error;
}

async function api(path, options = {}, kind = null, deadlineMs = readDeadlineMs) {
  const controller = new AbortController();
  let deadlineReached = false;
  const abortForSession = () => controller.abort(sessionController.signal.reason);
  if (sessionController.signal.aborted) abortForSession();
  else sessionController.signal.addEventListener("abort", abortForSession, {once: true});
  const timer = window.setTimeout(() => {
    deadlineReached = true;
    controller.abort(new DOMException("logical request deadline reached", "TimeoutError"));
  }, deadlineMs);
  const headers = new Headers(options.headers ?? {});
  headers.set("Accept", "application/json");
  headers.set("Authorization", `Bearer ${adminBearer}`);
  try {
    const response = await fetch(`${apiRoot}${path}`, {...options, cache: "no-store", credentials: "omit", headers, redirect: "error", referrerPolicy: "no-referrer", signal: controller.signal});
    const source = await response.text();
    if (deadlineReached) throw new AdminRequestTimeoutError("logical request deadline reached");
    if (!response.ok) {
      return {status: response.status, ok: false, value: null, problem: parseResponseProblem(path, response.status, source)};
    }
    if (kind === null) {
      if (source !== "") throw new AdminResponseError("unexpected mutation response body");
      return {status: response.status, ok: true, value: null, problem: null};
    }
    return {status: response.status, ok: true, value: parseAdminResponse(kind, source), problem: null};
  } catch (error) {
    if (deadlineReached) throw new AdminRequestTimeoutError("logical request deadline reached", {cause: error});
    throw error;
  } finally {
    window.clearTimeout(timer);
    sessionController.signal.removeEventListener("abort", abortForSession);
  }
}
