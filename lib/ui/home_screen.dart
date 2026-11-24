import 'package:flutter/material.dart';

import '../services/spotcast.dart';
import '../theme.dart';
import 'now_playing_sheet.dart';
import 'setup_screen.dart';
import 'shell.dart';
import 'widgets.dart';

class HomeScreen extends StatelessWidget {
  final AppState app;
  const HomeScreen({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    final s = app.spot;
    return RefreshIndicator(
      onRefresh: s.refresh,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
        children: [
          Row(children: [
            const Expanded(
              child: Text('Living Room',
                  style: TextStyle(fontSize: 30, fontWeight: FontWeight.w800)),
            ),
            if (s.connecting)
              const StatusPill('Connecting', color: Colors.white54)
            else if (s.online && s.reconnecting)
              const StatusPill('Reconnecting…', color: Colors.orangeAccent)
            else if (s.online && s.remote)
              const StatusPill('Away · via Tailscale', color: FireflyTheme.amber)
            else if (!s.online)
              const StatusPill('Bridge offline', color: Colors.redAccent)
            else if (s.receiver['reachable'] == false)
              const StatusPill('Receiver offline', color: Colors.orangeAccent)
            else
              StatusPill(s.receiverOn ? 'Receiver on' : 'Standby',
                  color: s.receiverOn ? FireflyTheme.spotify : Colors.white54),
          ]),
          const SizedBox(height: 4),
          Text('Yamaha RX-V671',
              style: TextStyle(color: Colors.white.withValues(alpha: 0.5))),
          if (!s.online && !s.connecting) _OfflineCard(app: app),
          if (s.online) ...[
            _SpotifyCard(app: app),
            _ReceiverCard(spot: s),
            _QuickActions(spot: s),
            _HealthStrip(spot: s),
          ],
        ],
      ),
    );
  }
}

class _OfflineCard extends StatelessWidget {
  final AppState app;
  const _OfflineCard({required this.app});

  @override
  Widget build(BuildContext context) => Panel(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Row(children: [
            Icon(Icons.cloud_off, color: Colors.redAccent),
            SizedBox(width: 10),
            Text('Can\'t reach the speaker bridge',
                style: TextStyle(fontWeight: FontWeight.w600)),
          ]),
          const SizedBox(height: 8),
          Text(
            'Make sure you\'re on home Wi-Fi. Radio and your own files still work '
            'from the other tabs.',
            style: TextStyle(color: Colors.white.withValues(alpha: 0.65)),
          ),
          const SizedBox(height: 12),
          Wrap(spacing: 8, children: [
            FilledButton.tonal(onPressed: app.spot.start, child: const Text('Try again')),
            OutlinedButton(
              onPressed: () => openSetup(context, app),
              child: const Text('Run setup check'),
            ),
          ]),
        ]),
      );
}

class _SpotifyCard extends StatelessWidget {
  final AppState app;
  const _SpotifyCard({required this.app});

  @override
  Widget build(BuildContext context) {
    final s = app.spot;
    final active = s.input == 'PC' && (s.playing || s.title != null);
    return Card(
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: active ? () => showNowPlaying(context, app) : null,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Row(children: [
            ArtThumb(url: s.art, size: 72, accent: FireflyTheme.spotify, source: 'spotify'),
            const SizedBox(width: 14),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Row(children: [
                  const Icon(Icons.queue_music, size: 14, color: FireflyTheme.spotify),
                  const SizedBox(width: 4),
                  Text(s.playing
                      ? (s.playingAccount != null
                          ? 'PLAYING · ${(s.playingAccount!['display_name'] as String? ?? '').split(' ').first.toUpperCase()}'
                          : 'PLAYING ON SPOTIFY')
                      : 'SPOTIFY',
                      style: const TextStyle(
                          fontSize: 11,
                          letterSpacing: 1,
                          color: FireflyTheme.spotify,
                          fontWeight: FontWeight.w700)),
                ]),
                const SizedBox(height: 4),
                Text(active ? (s.title ?? 'Spotify') : 'Nothing playing',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w700)),
                Text(
                    active
                        ? s.artists
                        : 'In Spotify, pick “Living Room (RX-V671)”',
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: Colors.white.withValues(alpha: 0.6))),
              ]),
            ),
            if (active)
              IconButton(
                iconSize: 40,
                icon: Icon(s.playing
                    ? Icons.pause_circle_filled_rounded
                    : Icons.play_circle_fill_rounded),
                onPressed: s.playPause,
              ),
          ]),
        ),
      ),
    );
  }
}

class _ReceiverCard extends StatelessWidget {
  final SpotcastController spot;
  const _ReceiverCard({required this.spot});

  @override
  Widget build(BuildContext context) {
    final s = spot;
    return Panel(
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          const Icon(Icons.speaker_group_rounded),
          const SizedBox(width: 10),
          const Expanded(
              child: Text('Receiver', style: TextStyle(fontWeight: FontWeight.w700))),
          Switch(value: s.receiverOn, onChanged: s.power),
        ]),
        if (s.receiverOn) ...[
          ReceiverVolume(spot: s),
          const SizedBox(height: 4),
          Wrap(spacing: 8, runSpacing: 8, children: [
            for (final e in receiverInputs.entries)
              ChoiceChip(
                label: Text(e.value),
                selected: s.input == e.key,
                onSelected: (_) => s.selectInput(e.key),
              ),
          ]),
        ],
      ]),
    );
  }
}

class _QuickActions extends StatelessWidget {
  final SpotcastController spot;
  const _QuickActions({required this.spot});

  Future<void> _pickSleep(BuildContext context) async {
    final m = await showModalBottomSheet<int>(
      context: context,
      builder: (ctx) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          const ListTile(title: Text('Sleep timer', style: TextStyle(fontWeight: FontWeight.w700))),
          for (final m in const [15, 30, 45, 60, 90])
            ListTile(title: Text('$m minutes'), onTap: () => Navigator.pop(ctx, m)),
          if (spot.sleepSeconds > 0)
            ListTile(
                leading: const Icon(Icons.close),
                title: const Text('Cancel timer'),
                onTap: () => Navigator.pop(ctx, 0)),
        ]),
      ),
    );
    if (m != null) await spot.sleep(m);
  }

  @override
  Widget build(BuildContext context) {
    final s = spot;
    Widget action(IconData i, String label, String sub, VoidCallback onTap,
            {bool on = false}) =>
        Expanded(
          child: Card(
            color: on ? FireflyTheme.amber.withValues(alpha: 0.16) : null,
            child: InkWell(
              borderRadius: BorderRadius.circular(20),
              onTap: onTap,
              child: Padding(
                padding: const EdgeInsets.symmetric(vertical: 14, horizontal: 8),
                child: Column(children: [
                  Icon(i, color: on ? FireflyTheme.amber : null),
                  const SizedBox(height: 6),
                  Text(label,
                      textAlign: TextAlign.center,
                      style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13)),
                  Text(sub,
                      textAlign: TextAlign.center,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: 11, color: Colors.white.withValues(alpha: 0.55))),
                ]),
              ),
            ),
          ),
        );
    return Row(children: [
      action(Icons.bedtime_outlined, 'Sleep',
          s.sleepSeconds > 0 ? fmtDuration(s.sleepSeconds) : 'Off', () => _pickSleep(context),
          on: s.sleepSeconds > 0),
      const SizedBox(width: 8),
      action(Icons.tv_rounded, 'Hand back', 'Back to TV / off', () async {
        await s.handBack();
        if (context.mounted) {
          ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
              content: Text('Paused; receiver goes back to its previous input')));
        }
      }),
      const SizedBox(width: 8),
      action(Icons.person_add_alt_1_rounded, 'Add me',
          s.addMeSeconds > 0 ? 'Visible ${fmtDuration(s.addMeSeconds)}' : '${s.accounts} linked',
          s.showAddMe,
          on: s.addMeSeconds > 0),
    ]);
  }
}

class _HealthStrip extends StatelessWidget {
  final SpotcastController spot;
  const _HealthStrip({required this.spot});

  @override
  Widget build(BuildContext context) {
    final s = spot;
    final buf = s.latency['receiver_buffer_s'];
    final q = s.latency['pi_queue_ms'];
    final stalls = s.health['stalls'] ?? 0;
    final upd = s.update;
    final updText = switch (upd['result']) {
      'updated' => 'Updated ${fmtAgo(upd['checked'])}',
      'current' => 'Up to date · checked ${fmtAgo(upd['checked'])}',
      'deferred-playing' => 'Update waiting for a pause',
      'download-failed' => 'Update check failed ${fmtAgo(upd['checked'])}',
      _ => 'Auto-updates daily',
    };
    final frozen = s.health['receiver_ok'] == false;
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Wrap(spacing: 8, runSpacing: 8, children: [
        if (frozen)
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: Colors.redAccent.withOpacity(0.15),
              borderRadius: BorderRadius.circular(12),
              border: Border.all(color: Colors.redAccent),
            ),
            child: const Row(children: [
              Icon(Icons.power_off, color: Colors.redAccent),
              SizedBox(width: 10),
              Expanded(
                child: Text("The receiver's network control is frozen. Unplug it from the "
                    'wall for 30 seconds, then plug it back in.'),
              ),
            ]),
          ),
        StatusPill(buf == null ? 'Delay –' : 'Delay ${((buf as num) * 1000).round() + ((q ?? 0) as num).round()} ms',
            color: Colors.lightBlueAccent),
        StatusPill(stalls == 0 ? 'No dropouts' : '$stalls dropouts',
            color: stalls == 0 ? FireflyTheme.spotify : Colors.orangeAccent),
        StatusPill(updText, color: upd['result'] == 'download-failed'
            ? Colors.orangeAccent
            : Colors.white70),
      ]),
    );
  }
}
