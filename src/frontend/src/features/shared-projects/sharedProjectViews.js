const segment = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value);
const revision = (value, minimum = 1) => Number.isSafeInteger(value) && value >= minimum;
const invalid = () => { throw new Error('共享项目资料未核对，请重新读取'); };
export function sharedProjectView(row) {
  if (!row || row.shared !== true || !segment(row.id) || !segment(row.owner_user_id)
      || !segment(row.actor_user_id) || typeof row.name !== 'string' || !row.name.trim()
      || !revision(row.revision) || !['active', 'provisioning', 'deleting'].includes(row.state)
      || !['owner', 'member', 'viewer', 'admin'].includes(row.role)
      || typeof row.can_write !== 'boolean' || typeof row.can_manage !== 'boolean'
      || row.can_write !== ['owner','member','admin'].includes(row.role)
      || row.can_manage !== ['owner','admin'].includes(row.role)
      || !row.authority || Object.keys(row.authority).sort().join(',') !== 'account_revision,member_revision,project_revision'
      || !revision(row.authority.project_revision) || !revision(row.authority.member_revision, 0)
      || !revision(row.authority.account_revision)) invalid();
  if (row.authority.project_revision !== row.revision) invalid();
  return row;
}
export function sharedProjectList(rows) {
  if (!Array.isArray(rows) || new Set(rows.map(row => row?.id)).size !== rows.length) invalid();
  rows.forEach(sharedProjectView);
  return rows;
}
export function sharedProjectDetail(data, projectId) {
  if (sharedProjectView(data?.project).id !== projectId || !Array.isArray(data.members) || !Array.isArray(data.invitations)
      || new Set(data.members.map(member => member?.user_id)).size !== data.members.length) invalid();
  for (const member of data.members) if (!segment(member.user_id) || member.project_id !== projectId
      || !revision(member.revision) || !['owner', 'member', 'viewer'].includes(member.role)) invalid();
  sharedInvitations(data.invitations);
  if (data.invitations.some(invitation => invitation.project_id !== projectId)) invalid();
  return data;
}
export function sharedDirectory(rows) {
  if (!Array.isArray(rows) || rows.some(row => !segment(row?.user_id) || typeof row.name !== 'string')
      || new Set(rows.map(row => row.user_id)).size !== rows.length) invalid();
  return rows;
}
export function sharedInvitations(rows) {
  if (!Array.isArray(rows) || rows.some(row => !segment(row?.id) || !segment(row.project_id)
      || !segment(row.to_user_id) || typeof row.project_name !== 'string' || !revision(row.revision)
      || row.state !== 'pending' || !['member', 'viewer'].includes(row.role))) invalid();
  return rows;
}
export function sharedActivity(rows) {
  if (!Array.isArray(rows) || rows.some(row => !segment(row?.id) || !segment(row.actor_user_id)
      || typeof row.action !== 'string' || typeof row.object_title !== 'string'
      || typeof row.at !== 'string' || !Number.isFinite(Date.parse(row.at)))) invalid();
  return rows;
}
export function sharedPending(rows, projectId) {
  if (!Array.isArray(rows) || rows.some(row => !segment(row?.operation_id) || row.project_id !== projectId
      || !segment(row.actor_user_id) || row.state !== 'pending' || !revision(row.revision)
      || !['create','invite','accept','role','remove','leave','transfer','delete'].includes(row.action)
      || !row.inputs || Array.isArray(row.inputs) || typeof row.inputs !== 'object')) invalid();
  return rows;
}
export function sharedInvitationTargets(users, detail) {
  return users.filter(user => !detail.members.some(member => member.user_id === user.user_id)
    && !detail.invitations.some(invitation => invitation.to_user_id === user.user_id));
}
export function canDecideSharedProject(project, member, action, users) {
  if (!project || project.state !== 'active') return false;
  if (action === 'leave') return Boolean(member && member.user_id === project.actor_user_id && member.role !== 'owner');
  if (!project.can_manage) return false;
  if (action === 'delete') return true;
  if (!member || member.role === 'owner') return false;
  if (action === 'remove') return true;
  return action === 'transfer' && users.some(user=>user.user_id===member.user_id);
}
