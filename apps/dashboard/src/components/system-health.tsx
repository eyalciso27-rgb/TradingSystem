import { HealthScreen } from "./operations-screens";
import { operationsDataSource } from "../lib/operations";
export async function SystemHealth({ operator, role }: { operator: string; role: string }) { return <HealthScreen operator={operator} role={role} snapshot={await operationsDataSource.getSnapshot()} />; }
