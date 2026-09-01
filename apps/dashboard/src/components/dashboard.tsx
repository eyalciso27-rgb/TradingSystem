import { OperationsOverview } from "./operations-screens";
import { operationsDataSource } from "../lib/operations";
export async function Dashboard({ operator, role }: { operator: string; role: string }) { return <OperationsOverview operator={operator} role={role} snapshot={await operationsDataSource.getSnapshot()} />; }
