// Mock native modules for web
import { Platform } from 'react-native';

if (Platform.OS === 'web') {
  // Mock expo-camera
  jest = undefined;
}
