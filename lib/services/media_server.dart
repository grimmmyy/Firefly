import 'dart:async';
import 'dart:io';

import 'package:network_info_plus/network_info_plus.dart';
import 'package:shelf/shelf.dart';
import 'package:shelf/shelf_io.dart' as shelf_io;
import 'package:shelf_router/shelf_router.dart';

/// Minimal HTTP server that serves one local file at a time to the receiver.
/// Supports HTTP Range requests, which most DLNA renderers (RX-V671 included)
/// rely on when seeking or buffering.
class MediaServer {
  HttpServer? _server;
  final Map<String, _Served> _files = {};
  String? _lanIp;

  Future<String> ensureStarted() async {
    if (_server != null && _lanIp != null) {
      return 'http://$_lanIp:${_server!.port}';
    }
    _lanIp = await _discoverLanIp();
    final router = Router();
    router.get('/media/<id>', _serveFile);
    router.head('/media/<id>', _serveFile);
    _server = await shelf_io.serve(
      router.call,
      InternetAddress.anyIPv4,
      0,
    );
    return 'http://$_lanIp:${_server!.port}';
  }

  Future<String> publish(File file, {String? mime}) async {
    final base = await ensureStarted();
    final id = DateTime.now().microsecondsSinceEpoch.toString();
    _files[id] = _Served(file, mime ?? _guessMime(file.path));
    return '$base/media/$id';
  }

  Future<void> stop() async {
    await _server?.close(force: true);
    _server = null;
    _files.clear();
  }

  Future<Response> _serveFile(Request req, String id) async {
    final served = _files[id];
    if (served == null) return Response.notFound('unknown media');
    final file = served.file;
    if (!await file.exists()) return Response.notFound('file gone');
    final length = await file.length();

    final range = req.headers['range'];
    if (range != null && range.startsWith('bytes=')) {
      final spec = range.substring('bytes='.length);
      final parts = spec.split('-');
      final start = int.tryParse(parts[0]) ?? 0;
      final end =
          parts.length > 1 && parts[1].isNotEmpty
              ? int.tryParse(parts[1]) ?? (length - 1)
              : (length - 1);
      final safeEnd = end.clamp(start, length - 1);
      final body = file.openRead(start, safeEnd + 1);
      return Response(
        206,
        body: body,
        headers: {
          'Content-Type': served.mime,
          'Content-Length': '${safeEnd - start + 1}',
          'Content-Range': 'bytes $start-$safeEnd/$length',
          'Accept-Ranges': 'bytes',
          'transferMode.dlna.org': 'Streaming',
          'contentFeatures.dlna.org':
              'DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000',
        },
      );
    }

    if (req.method == 'HEAD') {
      return Response.ok(
        null,
        headers: {
          'Content-Type': served.mime,
          'Content-Length': '$length',
          'Accept-Ranges': 'bytes',
        },
      );
    }

    return Response.ok(
      file.openRead(),
      headers: {
        'Content-Type': served.mime,
        'Content-Length': '$length',
        'Accept-Ranges': 'bytes',
      },
    );
  }

  Future<String> _discoverLanIp() async {
    try {
      final info = NetworkInfo();
      final wifiIp = await info.getWifiIP();
      if (wifiIp != null && wifiIp.isNotEmpty) return wifiIp;
    } catch (_) {}
    for (final iface in await NetworkInterface.list(
      type: InternetAddressType.IPv4,
      includeLoopback: false,
      includeLinkLocal: false,
    )) {
      for (final addr in iface.addresses) {
        if (!addr.isLoopback) return addr.address;
      }
    }
    return '127.0.0.1';
  }

  String _guessMime(String path) {
    final lower = path.toLowerCase();
    if (lower.endsWith('.mp3')) return 'audio/mpeg';
    if (lower.endsWith('.m4a') || lower.endsWith('.aac')) return 'audio/aac';
    if (lower.endsWith('.flac')) return 'audio/flac';
    if (lower.endsWith('.wav')) return 'audio/wav';
    if (lower.endsWith('.ogg') || lower.endsWith('.oga')) return 'audio/ogg';
    if (lower.endsWith('.opus')) return 'audio/opus';
    return 'audio/mpeg';
  }
}

class _Served {
  final File file;
  final String mime;
  _Served(this.file, this.mime);
}
