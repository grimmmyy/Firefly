import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:xml/xml.dart';

/// A discovered UPnP MediaRenderer on the LAN.
class Renderer {
  final String friendlyName;
  final String manufacturer;
  final String modelName;
  final Uri location;
  final Uri avTransportControlUrl;
  final Uri renderingControlUrl;

  Renderer({
    required this.friendlyName,
    required this.manufacturer,
    required this.modelName,
    required this.location,
    required this.avTransportControlUrl,
    required this.renderingControlUrl,
  });

  String get uniqueKey => location.toString();

  @override
  String toString() => '$friendlyName ($modelName)';
}

/// SSDP (Simple Service Discovery Protocol) search for UPnP MediaRenderer devices.
/// Sends M-SEARCH to 239.255.255.250:1900 and parses the LOCATION of every
/// response that advertises `urn:schemas-upnp-org:device:MediaRenderer:1`.
class SsdpDiscovery {
  static const _multicastAddress = '239.255.255.250';
  static const _port = 1900;
  static const _target = 'urn:schemas-upnp-org:device:MediaRenderer:1';

  /// Discover renderers within [timeout]. Deduplicated by device description URL.
  Future<List<Renderer>> discover({
    Duration timeout = const Duration(seconds: 5),
    bool sweep = false,
  }) async {
    final socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0);
    socket.readEventsEnabled = true;
    socket.broadcastEnabled = true;

    final request =
        'M-SEARCH * HTTP/1.1\r\n'
        'HOST: $_multicastAddress:$_port\r\n'
        'MAN: "ssdp:discover"\r\n'
        'MX: 3\r\n'
        'ST: $_target\r\n\r\n';

    final bytes = utf8.encode(request);
    socket.send(bytes, InternetAddress(_multicastAddress), _port);
    // Resend once for packet loss.
    await Future.delayed(const Duration(milliseconds: 500));
    socket.send(bytes, InternetAddress(_multicastAddress), _port);

    final locations = <String>{};
    final completer = Completer<void>();
    late StreamSubscription sub;
    sub = socket.listen((event) {
      if (event == RawSocketEvent.read) {
        final dg = socket.receive();
        if (dg == null) return;
        final text = utf8.decode(dg.data, allowMalformed: true);
        final loc = _headerValue(text, 'LOCATION');
        if (loc != null) locations.add(loc.trim());
      }
    });

    await Future.any([
      Future.delayed(timeout),
      completer.future,
    ]);
    await sub.cancel();
    socket.close();

    // iOS blocks multicast without Apple's restricted entitlement, and Android
    // often drops replies, so also try known/likely description URLs directly.
    locations.addAll(extraLocations);

    final renderers = <Renderer>[];
    Future<void> describeAll(Iterable<String> locs) async {
      for (final loc in locs) {
        try {
          final r = await _describe(Uri.parse(loc));
          if (r != null) renderers.add(r);
        } catch (_) {
          // Ignore devices that fail to describe; they won't be usable anyway.
        }
      }
    }

    await describeAll(locations);
    // The subnet sweep opens a bare TCP connection to every host's renderer
    // port and drops it without a request. The RX-V671's 2011 network stack
    // is suspected of wedging on that, so it is a last resort: only when
    // nothing was found the normal way, or when asked for explicitly.
    if (renderers.isEmpty || sweep) {
      final more = (await _sweepSubnet()).difference(locations);
      await describeAll(more);
    }
    // Deduplicate by friendlyName+host.
    final seen = <String>{};
    return renderers.where((r) {
      final k = '${r.friendlyName}|${r.location.host}';
      return seen.add(k);
    }).toList();
  }

  /// Description URLs to always probe (saved/manual receivers).
  Set<String> extraLocations = {};

  /// Well-known description paths for common renderers.
  static const probePaths = [
    ':49154/MediaRenderer/desc.xml', // Yamaha (RX-V671 etc.)
    ':8080/MediaRenderer/desc.xml',
    ':9197/dmr', // Samsung TVs
  ];

  /// Build candidate description URLs for a host or IP the user typed.
  static List<String> candidatesFor(String hostOrUrl) {
    final h = hostOrUrl.trim();
    if (h.startsWith('http')) return [h];
    return probePaths.map((p) => 'http://$h$p').toList();
  }

  /// Unicast TCP sweep of the local /24 for known renderer ports.
  Future<Set<String>> _sweepSubnet() async {
    final found = <String>{};
    String? prefix;
    for (final iface in await NetworkInterface.list(
        type: InternetAddressType.IPv4, includeLinkLocal: false)) {
      for (final a in iface.addresses) {
        final ip = a.address;
        if (ip.startsWith('192.168.') || ip.startsWith('10.') ||
            ip.startsWith('172.')) {
          prefix ??= ip.substring(0, ip.lastIndexOf('.') + 1);
        }
      }
    }
    if (prefix == null) return found;
    const ports = {49154: '/MediaRenderer/desc.xml', 9197: '/dmr'};
    // Batched to stay well under iOS's per-process socket limit.
    final targets = <MapEntry<String, MapEntry<int, String>>>[];
    for (var i = 1; i < 255; i++) {
      for (final e in ports.entries) {
        targets.add(MapEntry('$prefix$i', e));
      }
    }
    for (var start = 0; start < targets.length; start += 64) {
      final batch = targets.skip(start).take(64);
      await Future.wait(batch.map((t) => Socket.connect(t.key, t.value.key,
                  timeout: const Duration(milliseconds: 600))
              .then((s) {
            s.destroy();
            found.add('http://${t.key}:${t.value.key}${t.value.value}');
          }).catchError((_) {})));
    }
    return found;
  }

  String? _headerValue(String response, String name) {
    for (final line in const LineSplitter().convert(response)) {
      final idx = line.indexOf(':');
      if (idx < 0) continue;
      if (line.substring(0, idx).trim().toUpperCase() == name.toUpperCase()) {
        return line.substring(idx + 1).trim();
      }
    }
    return null;
  }

  Future<Renderer?> _describe(Uri location) async {
    final resp = await http
        .get(location)
        .timeout(const Duration(seconds: 4));
    if (resp.statusCode != 200) return null;
    final doc = XmlDocument.parse(resp.body);
    final device = doc.findAllElements('device').firstOrNull;
    if (device == null) return null;
    final friendlyName =
        device.findElements('friendlyName').firstOrNull?.innerText ?? 'Unknown';
    final manufacturer =
        device.findElements('manufacturer').firstOrNull?.innerText ?? '';
    final modelName =
        device.findElements('modelName').firstOrNull?.innerText ?? '';

    Uri? avTransportControl;
    Uri? renderingControl;
    for (final svc in doc.findAllElements('service')) {
      final type = svc.findElements('serviceType').firstOrNull?.innerText ?? '';
      final control =
          svc.findElements('controlURL').firstOrNull?.innerText ?? '';
      final controlUrl = location.resolve(control);
      if (type.contains('AVTransport')) avTransportControl = controlUrl;
      if (type.contains('RenderingControl')) renderingControl = controlUrl;
    }
    if (avTransportControl == null || renderingControl == null) return null;
    return Renderer(
      friendlyName: friendlyName,
      manufacturer: manufacturer,
      modelName: modelName,
      location: location,
      avTransportControlUrl: avTransportControl,
      renderingControlUrl: renderingControl,
    );
  }
}
