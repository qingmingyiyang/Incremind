import { Switch } from '../../shared/ui';

export function EgressSwitch(props) {
  return <span className="settings-egress-control">
    <span aria-hidden="true">允许外发</span>
    <Switch {...props} className="settings-egress-toggle"/>
  </span>;
}
