export const adminRoutes = [
  { id: "overview", label: "개요", href: "#overview" },
  { id: "routing", label: "라우팅", href: "#routing" },
  { id: "clients", label: "접속 키", href: "#clients" },
  { id: "models", label: "모델", href: "#models" },
  { id: "evidence", label: "증거", href: "#evidence" },
] as const;

export type AdminRouteId = (typeof adminRoutes)[number]["id"];
