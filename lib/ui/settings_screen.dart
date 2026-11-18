import 'package:flutter/material.dart';

import '../services/ssdp.dart';
import '../theme.dart';
import 'shell.dart';
import 'widgets.dart';
import 'account_tile.dart';

class SettingsScreen extends StatelessWidget {
  final AppState app;
  const SettingsScreen({super.key, required this.app});

  Future<String?> _ask(BuildContext context, String title, String initial) {
    final ctrl = TextEditingController(text: initial);
    return showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(title),
        content: TextField(controller: ctrl, autofocus: true),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(ctx, ctrl.text), child: const Text('Save')),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final c = app.cast;
    final s = app.spot;
    final devices = s.devices;
    final reg = (devices['registered'] ?? const {}) as Map;
    final unknown = (devices['unknown'] ?? const {}) as Map;
    final ignored = (devices['ignored'] ?? const {}) as Map;
    final upd = s.update;

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
      children: [
        const Text('Settings', style: TextStyle(fontSize: 30, fontWeight: FontWeight.w800)),

        // ---------------------------------------------------------- people
        SectionLabel('Spotify accounts · ${s.accounts} linked',
            trailing: TextButton(onPressed: s.showAddMe, child: const Text('Show Add me'))),
        Card(
          child: Column(children: [
            if (s.accountsList.isEmpty)
              const ListTile(
                leading: Icon(Icons.person_off_outlined),
                title: Text('No accounts linked yet'),
                subtitle: Text('In Spotify on home Wi-Fi, pick "Living Room - Add me".'),
              ),
            for (final a in s.accountsList) AccountTile(account: a, spot: s),
            const Divider(height: 1),
            const ListTile(
              dense: true,
              leading: Icon(Icons.info_outline),
              title: Text('How people join'),
              subtitle: Text(
                  'A new phone opening Spotify on home Wi-Fi sees "Living Room - Add me". '
                  'Picking it once links that account for good. For Alexa, link the same '
                  'Spotify account in the Alexa app under that person’s voice profile.'),
            ),
          ]),
        ),
        if (unknown.isNotEmpty || reg.isNotEmpty || ignored.isNotEmpty) ...[
          const SectionLabel('Devices'),
          Card(
            child: Column(children: [
              for (final e in unknown.entries)
                ListTile(
                  leading: const Icon(Icons.devices_other, color: Colors.orangeAccent),
                  title: Text('Unregistered device ${(e.value as Map)['ip'] ?? ''}'),
                  subtitle: Text('${e.key} · keeps "Add me" visible'),
                  trailing: TextButton(onPressed: () => s.ignore(e.key), child: const Text('Ignore')),
                ),
              for (final e in reg.entries)
                ListTile(
                  leading: const Icon(Icons.phone_iphone, color: FireflyTheme.spotify),
                  title: Text(s.displayNameFor((e.value as Map)['account'] as String?) != null
                      ? "${s.displayNameFor((e.value as Map)['account'] as String?)}’s device"
                      : 'Registered device'),
                  subtitle: Text('${(e.value as Map)['ip'] ?? ''} · ${e.key}'),
                  trailing: TextButton(onPressed: () => s.forget(e.key), child: const Text('Forget')),
                ),
              for (final e in ignored.entries)
                ListTile(
                  leading: const Icon(Icons.block, color: Colors.white38),
                  title: Text('Ignored ${(e.value as Map)['ip'] ?? ''}'),
                  subtitle: Text(e.key),
                  trailing: TextButton(onPressed: () => s.forget(e.key), child: const Text('Un-ignore')),
                ),
            ]),
          ),
        ],

        // ---------------------------------------------------------- receiver (DLNA)
        SectionLabel('Receiver for radio & files',
            trailing: TextButton(
              onPressed: c.discovering ? null : c.discover,
              child: Text(c.discovering ? 'Scanning…' : 'Rescan'),
            )),
        Card(
          child: Column(children: [
            if (c.renderers.isEmpty)
              const ListTile(
                leading: Icon(Icons.search_off),
                title: Text('No receivers found'),
                subtitle: Text('Check the receiver is on and wired to the router'),
              ),
            for (final Renderer r in c.renderers)
              RadioListTile<String>(
                value: r.uniqueKey,
                groupValue: c.selected?.uniqueKey,
                onChanged: (_) => c.select(r),
                title: Text(r.friendlyName),
                subtitle: Text('${r.modelName} · ${r.location.host}'),
              ),
            ListTile(
              leading: const Icon(Icons.edit),
              title: const Text('Add receiver by IP'),
              onTap: () async {
                final ip = await _ask(context, 'Receiver IP address', '192.168.1.7');
                if (ip != null && ip.trim().isNotEmpty) await c.addReceiverByIp(ip);
              },
            ),
          ]),
        ),

        // ---------------------------------------------------------- bridge
        const SectionLabel('Speaker bridge (Raspberry Pi)'),
        Card(
          child: Column(children: [
            ListTile(
              leading: Icon(Icons.router,
                  color: s.online ? FireflyTheme.spotify : Colors.redAccent),
              title: Text(s.online ? 'Connected' : (s.error ?? 'Not connected')),
              subtitle: Text(s.host ?? 'not found'),
              trailing: TextButton(
                child: const Text('Change'),
                onPressed: () async {
                  final h = await _ask(context, 'Bridge address', s.host ?? '192.168.1.110');
                  if (h != null && h.trim().isNotEmpty) await s.setHost(h);
                },
              ),
            ),
            ListTile(
              leading: const Icon(Icons.system_update_alt),
              title: const Text('Spotify player auto-update'),
              subtitle: Text(
                  'Checks daily · last check ${fmtAgo(upd['checked'])} '
                  '(${upd['result'] ?? 'pending'})\n'
                  '${upd['version'] ?? ''}'),
              isThreeLine: true,
              trailing: TextButton(onPressed: s.updateNow, child: const Text('Check now')),
            ),
            ListTile(
              leading: const Icon(Icons.timer_outlined),
              title: const Text('Hand back when idle'),
              subtitle: Text(
                  'After ${s.idleHandbackMin} min of no Spotify, the receiver returns to '
                  'its previous input (or standby)'),
            ),
            ListTile(
              leading: const Icon(Icons.speed),
              title: const Text('Latency'),
              subtitle: Text(
                  'Receiver buffer ${s.latency['receiver_buffer_s'] ?? '–'} s '
                  '(target ${s.latency['target_s'] ?? '–'} s) · Pi queue '
                  '${s.latency['pi_queue_ms'] ?? '–'} ms\n'
                  'Dropouts ${s.health['stalls'] ?? 0} · reconnects ${s.health['reconnects'] ?? 0}'),
              isThreeLine: true,
            ),
          ]),
        ),

        // ---------------------------------------------------------- about
        const SectionLabel('About'),
        const Card(
          child: ListTile(
            leading: Icon(Icons.local_fire_department, color: FireflyTheme.amber),
            title: Text('Firefly'),
            subtitle: Text('Spotify, radio and your music on the Yamaha RX-V671'),
          ),
        ),
      ],
    );
  }
}
