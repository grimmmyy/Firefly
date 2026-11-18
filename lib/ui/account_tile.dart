import 'package:flutter/material.dart';

import '../services/spotcast.dart';
import '../theme.dart';

/// One linked Spotify account: avatar, display name, @username, live status.
class AccountTile extends StatelessWidget {
  final Map account;
  final SpotcastController spot;
  const AccountTile({super.key, required this.account, required this.spot});

  String get _name => (account['display_name'] as String?) ?? 'Spotify account';
  String get _user => (account['username'] as String?) ?? '';

  Future<void> _confirmUnlink(BuildContext context) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Unlink $_name?'),
        content: Text(
            '@$_user will no longer see "Living Room (RX-V671)" in Spotify. They can '
            're-link any time by picking "Living Room - Add me".'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(
              style: FilledButton.styleFrom(backgroundColor: Colors.redAccent),
              onPressed: () => Navigator.pop(ctx, true),
              child: const Text('Unlink')),
        ],
      ),
    );
    if (ok == true) await spot.unlinkAccount((account['slot'] as num).toInt());
  }

  @override
  Widget build(BuildContext context) {
    final image = account['image'] as String?;
    final onReceiver = account['on_receiver'] == true;
    final owner = account['follow_owner'] == true;
    final initials = _name.trim().isEmpty ? '?' : _name.trim()[0].toUpperCase();
    final avatar = CircleAvatar(
      radius: 22,
      backgroundColor: FireflyTheme.spotify.withValues(alpha: 0.25),
      foregroundImage: image != null && image.isNotEmpty ? NetworkImage(image) : null,
      child: Text(initials,
          style: const TextStyle(fontWeight: FontWeight.w700, color: FireflyTheme.spotify)),
    );
    final devices = (account['devices'] as List?)?.length ?? 0;
    final hasChips = onReceiver || owner || devices > 0;
    return ListTile(
      leading: avatar,
      title: Text(_name, maxLines: 1, overflow: TextOverflow.ellipsis),
      subtitle: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text('@$_user',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(fontSize: 12, color: Colors.white.withValues(alpha: 0.55))),
        if (hasChips)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Wrap(spacing: 6, runSpacing: 4, children: [
              if (onReceiver) _chip('On receiver', FireflyTheme.spotify),
              if (owner) _chip('Alexa follow', FireflyTheme.amber),
              if (devices > 0) _chip('$devices device${devices == 1 ? '' : 's'}', Colors.white54),
            ]),
          ),
      ]),
      isThreeLine: hasChips,
      trailing: PopupMenuButton<String>(
        onSelected: (v) {
          if (v == 'unlink') _confirmUnlink(context);
        },
        itemBuilder: (_) => const [
          PopupMenuItem(value: 'unlink', child: Text('Unlink account')),
        ],
      ),
    );
  }

  Widget _chip(String t, Color c) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
        decoration: BoxDecoration(
            color: c.withValues(alpha: 0.15), borderRadius: BorderRadius.circular(10)),
        child: Text(t, style: TextStyle(fontSize: 11, color: c, fontWeight: FontWeight.w600)),
      );
}
