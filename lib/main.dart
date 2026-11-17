import 'package:flutter/material.dart';
import 'ui/home_page.dart';
import 'services/cast_controller.dart';

void main() {
  runApp(const FireflyApp());
}

class FireflyApp extends StatelessWidget {
  const FireflyApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Firefly',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFFFFA726),
          brightness: Brightness.dark,
        ),
        useMaterial3: true,
      ),
      home: HomePage(controller: CastController()),
    );
  }
}
