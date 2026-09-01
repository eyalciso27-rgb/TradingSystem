import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { SystemHealth } from "../../components/system-health";
import { SESSION_COOKIE, verifySessionToken } from "../../lib/auth";
export const dynamic = "force-dynamic";
export default async function HealthPage() { const store = await cookies(); const session = verifySessionToken(store.get(SESSION_COOKIE)?.value, Math.floor(Date.now() / 1000)); if (!session) redirect("/login"); return <SystemHealth operator={session.operator} role={session.role} />; }
