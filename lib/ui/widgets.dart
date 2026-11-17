import 'package:flutter/material.dart';

import '../services/spotcast.dart';
import '../theme.dart';

/// Receiver master volume in real dB (0.5 dB steps), capped at the bridge's
/// safety ceiling. Falls back to a hint when the bridge is offline.
class ReceiverVolume extends StatelessWidget {
  final SpotcastController spot;
  const ReceiverVolume({super.key, required this.spot});

  @override
  Widget build(BuildContext context) {
    final db = spot.volumeDb;
    if (!spot.online || db == null) {
      return const SizedBox.shrink();
    }
    const minDb = -80.5;
    final maxDb = spot.maxDb;
    final v = db.clamp(minDb, maxDb);
    return Row(children: [
      IconButton(
        tooltip: spot.muted ? 'Unmute' : 'Mute',
        icon: Icon(spot.muted ? Icons.volume_off_rounded : Icons.volume_down_rounded),
        onPressed: () => spot.mute(!spot.muted),
      ),
      Expanded(
        child: Slider(
          min: minDb,
          max: maxDb,
          divisions: ((maxDb - minDb) * 2).round(),
          value: v,
          label: '${v.toStringAsFixed(1)} dB',
          onChanged: spot.receiverOn ? spot.previewVolume : null,
          onChangeEnd: spot.receiverOn ? spot.setVolume : null,
        ),
      ),
      SizedBox(
        width: 64,
        child: Text('${v.toStringAsFixed(1)} dB',
            textAlign: TextAlign.right,
            style: TextStyle(
                fontFeatures: const [FontFeature.tabularFigures()],
                color: Colors.white.withValues(alpha: 0.7))),
      ),
    ]);
  }
}

/// A rounded card with an optional title row.
class Panel extends StatelessWidget {
  final Widget child;
  final EdgeInsets padding;
  const Panel({super.key, required this.child, this.padding = const EdgeInsets.all(16)});

  @override
  Widget build(BuildContext context) =>
      Card(child: Padding(padding: padding, child: child));
}

/// Small coloured status dot + label.
class StatusPill extends StatelessWidget {
  final String text;
  final Color color;
  const StatusPill(this.text, {super.key, this.color = FireflyTheme.spotify});

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
        decoration: BoxDecoration(
          color: color.withValues(alpha: 0.15),
          borderRadius: BorderRadius.circular(20),
        ),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Container(
              width: 7,
              height: 7,
              decoration: BoxDecoration(color: color, shape: BoxShape.circle)),
          const SizedBox(width: 6),
          Text(text, style: TextStyle(color: color, fontSize: 12, fontWeight: FontWeight.w600)),
        ]),
      );
}

String fmtDuration(int seconds) {
  final m = seconds ~/ 60, s = seconds % 60;
  return m >= 60 ? '${m ~/ 60}h ${m % 60}m' : '$m:${s.toString().padLeft(2, '0')}';
}

String fmtAgo(num? epoch) {
  if (epoch == null || epoch == 0) return 'never';
  final d = DateTime.now().difference(
      DateTime.fromMillisecondsSinceEpoch((epoch * 1000).toInt()));
  if (d.inMinutes < 1) return 'just now';
  if (d.inHours < 1) return '${d.inMinutes} min ago';
  if (d.inDays < 1) return '${d.inHours} h ago';
  return '${d.inDays} d ago';
}
