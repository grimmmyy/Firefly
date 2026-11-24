import 'dart:async';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:record/record.dart';

import 'shell.dart';

/// Room EQ: the Pi plays a 1 kHz marker then one tone per EQ band through the
/// receiver; the phone's microphone measures each band, and the Pi flattens the
/// response (peaks cut, dips boosted at most +4 dB).
class RoomEqPage extends StatefulWidget {
  final AppState app;
  const RoomEqPage({super.key, required this.app});

  @override
  State<RoomEqPage> createState() => _RoomEqPageState();
}

class _RoomEqPageState extends State<RoomEqPage> {
  static const _rate = 44100;
  final _rec = AudioRecorder();
  StreamSubscription<Uint8List>? _sub;
  String _msg = 'Put the phone where you usually sit, at ear height, and keep the room quiet.';
  bool _running = false;
  List<double>? _levels;
  List<double>? _eq;
  List<int> _bandHz = [];

  // mic RMS per 20 ms block, in dBFS
  final List<double> _blocks = [];
  final _pending = BytesBuilder();
  static const _blockSamples = _rate ~/ 50;

  @override
  void dispose() {
    _sub?.cancel();
    _rec.dispose();
    super.dispose();
  }

  void _onPcm(Uint8List data) {
    _pending.add(data);
    final bytes = _pending.takeBytes();
    final usable = bytes.length - bytes.length % (_blockSamples * 2);
    final bd = ByteData.sublistView(bytes);
    for (var off = 0; off < usable; off += _blockSamples * 2) {
      var sum = 0.0;
      for (var i = 0; i < _blockSamples; i++) {
        final v = bd.getInt16(off + i * 2, Endian.little) / 32768.0;
        sum += v * v;
      }
      final rms = math.sqrt(sum / _blockSamples);
      _blocks.add(20 * math.log(math.max(rms, 1e-6)) / math.ln10);
    }
    if (usable < bytes.length) _pending.add(bytes.sublist(usable));
  }

  Future<void> _measure() async {
    if (!await _rec.hasPermission()) {
      setState(() => _msg = 'Microphone permission is needed to measure the room.');
      return;
    }
    setState(() {
      _running = true;
      _levels = null;
      _eq = null;
      _blocks.clear();
      _msg = 'Listening… the receiver will play a beep, then a series of tones (~25 s).';
    });
    final stream = await _rec.startStream(const RecordConfig(
      encoder: AudioEncoder.pcm16bits,
      sampleRate: _rate,
      numChannels: 1,
      autoGain: false,
      echoCancel: false,
      noiseSuppress: false,
    ));
    _sub = stream.listen(_onPcm);
    await Future.delayed(const Duration(milliseconds: 600));
    final plan = await widget.app.spot.x('roomeq/start');
    if (plan == null) {
      await _stop();
      setState(() {
        _running = false;
        _msg = 'Couldn\'t reach the speaker bridge.';
      });
      return;
    }
    final total = (plan['total_s'] as num).toDouble();
    await Future.delayed(Duration(milliseconds: ((total + 4) * 1000).round()));
    await _stop();
    _analyse(plan);
  }

  Future<void> _stop() async {
    await _rec.stop();
    await _sub?.cancel();
    _sub = null;
  }

  void _analyse(Map plan) {
    // noise floor = quietest 20% of the first second; marker onset = first block
    // 15 dB above it
    if (_blocks.length < 100) {
      setState(() {
        _running = false;
        _msg = 'The microphone recorded nothing. Try again.';
      });
      return;
    }
    final head = [..._blocks.take(40)]..sort();
    final floor = head[head.length ~/ 5];
    final onset = _blocks.indexWhere((v) => v > floor + 15);
    if (onset < 0) {
      setState(() {
        _running = false;
        _msg = 'Didn\'t hear the receiver. Is it on and turned up enough to hear clearly?';
      });
      return;
    }
    final bands = (plan['bands'] as List).cast<Map>();
    final levels = <double>[];
    for (final b in bands) {
      final start = (b['start'] as num).toDouble(), dur = (b['dur'] as num).toDouble();
      // middle 60% of the tone, in 20 ms blocks after the marker onset
      final from = onset + ((start + dur * 0.2) * 50).round();
      final to = onset + ((start + dur * 0.8) * 50).round();
      if (to >= _blocks.length) break;
      final seg = _blocks.sublist(from, to);
      // average power, not average dB
      final p = seg.map((d) => math.pow(10, d / 10)).reduce((x, y) => x + y) / seg.length;
      levels.add(10 * math.log(p) / math.ln10);
    }
    if (levels.length != bands.length) {
      setState(() {
        _running = false;
        _msg = 'The recording was cut short. Try again.';
      });
      return;
    }
    final weak = levels.where((l) => l < floor + 6).length;
    setState(() {
      _running = false;
      _levels = levels;
      _bandHz = bands.map((b) => (b['hz'] as num).toInt()).toList();
      _msg = weak > 3
          ? 'Several tones were barely louder than the room noise; turn the receiver up '
              'and measure again for a better result.'
          : 'Measured. Apply to set the room EQ.';
    });
  }

  Future<void> _apply() async {
    final r = await widget.app.spot.x('roomeq/apply',
        {'levels': _levels!.map((v) => v.toStringAsFixed(1)).join(',')});
    final eq = (r?['eq'] as List?)?.cast<num>().map((v) => v.toDouble()).toList();
    if (!mounted) return;
    if (eq == null) {
      setState(() => _msg = 'Couldn\'t apply: ${r?['error'] ?? 'bridge not reachable'}');
      return;
    }
    setState(() => _eq = eq);
    Navigator.pop(context, eq);
  }

  @override
  Widget build(BuildContext context) {
    final lv = _levels;
    final mx = lv == null ? 0.0 : lv.reduce(math.max);
    return Scaffold(
      appBar: AppBar(title: const Text('Measure my room')),
      body: ListView(padding: const EdgeInsets.all(16), children: [
        Text(_msg),
        const SizedBox(height: 16),
        if (_running) const LinearProgressIndicator(),
        if (lv != null)
          for (var i = 0; i < lv.length; i++)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 2),
              child: Row(children: [
                SizedBox(
                    width: 64,
                    child: Text(_bandHz[i] >= 1000
                        ? '${(_bandHz[i] / 1000).toStringAsFixed(1)} kHz'
                        : '${_bandHz[i]} Hz')),
                Expanded(
                  child: LinearProgressIndicator(
                    value: ((lv[i] - mx + 30) / 30).clamp(0.02, 1.0),
                    minHeight: 10,
                  ),
                ),
                SizedBox(
                    width: 56,
                    child: Text('${(lv[i] - mx).toStringAsFixed(1)} dB',
                        textAlign: TextAlign.end)),
              ]),
            ),
        const SizedBox(height: 16),
        const Text(
            'Phone microphones aren\'t calibrated, so corrections are kept gentle. '
            'You can fine-tune the bands afterwards.',
            style: TextStyle(color: Colors.white60)),
        const SizedBox(height: 16),
        Row(children: [
          FilledButton.icon(
            icon: const Icon(Icons.mic),
            label: Text(lv == null ? 'Start' : 'Measure again'),
            onPressed: _running ? null : _measure,
          ),
          const SizedBox(width: 12),
          if (lv != null && _eq == null)
            FilledButton.tonal(onPressed: _apply, child: const Text('Apply')),
        ]),
      ]),
    );
  }
}
