import { EmergencyReadOnlyScreen } from "./operations-screens";
import { operationsDataSource } from "../lib/operations";
export async function EmergencyControls({ operator, role }: { operator: string; role: string }) { return <EmergencyReadOnlyScreen operator={operator} role={role} snapshot={await operationsDataSource.getSnapshot()} />; }
