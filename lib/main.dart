import 'package:flutter/material.dart';

import 'services/cast_controller.dart';
import 'services/spotcast.dart';
import 'theme.dart';
import 'ui/shell.dart';

void main() {
  runApp(FireflyApp(app: AppState(CastController(), SpotcastController())));
}

class FireflyApp extends StatelessWidget {
  final AppState app;
  const FireflyApp({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Firefly',
      debugShowCheckedModeBanner: false,
      theme: FireflyTheme.dark(),
      darkTheme: FireflyTheme.dark(),
      themeMode: ThemeMode.dark,
      home: Shell(app: app),
    );
  }
}
