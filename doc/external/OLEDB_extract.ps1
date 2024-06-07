# ver=2022-01-25
# The comment above is used to later reset this file, do not modify it
$OleDbConn = New-Object "System.Data.OleDb.OleDbConnection";
$OleDbCmd = New-Object "System.Data.OleDb.OleDbCommand";
$OleDbAdapter = New-Object "System.Data.OleDb.OleDbDataAdapter";
$DataTable = New-Object "System.Data.DataTable";

$OleDbConn.ConnectionString = "Provider=PIOLEDB.1;Data Source=$($args[0]);Integrated Security=SSPI;Persist Security Info=False;Session ID=-1;Sync Calls=True;Time Zone = Server;Command Timeout=1000;";
$OleDbCmd.Connection = $OleDbConn;
$OleDbCmd.CommandText = "$($args[1])";

$OleDbAdapter.SelectCommand = $OleDbCmd;

$OleDbConn.Open();
$OleDbAdapter.Fill($DataTable) >$null| Out-Null;

$DataTable | ConvertTo-Csv -NoTypeInformation

$OleDbConn.Close();