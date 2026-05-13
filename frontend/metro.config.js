const { getDefaultConfig } = require('expo/metro-config');
const config = getDefaultConfig(__dirname);

config.resolver.resolveRequest = (context, moduleName, platform) => {
  if (platform === 'web') {
    const mocks = {
      'expo-camera': require.resolve('./src/mocks/native-mocks.js'),
      'expo-location': require.resolve('./src/mocks/native-mocks.js'),
      'expo-notifications': require.resolve('./src/mocks/native-mocks.js'),
      'expo-secure-store': require.resolve('./src/mocks/native-mocks.js'),
      'react-native-maps': require.resolve('./src/mocks/native-mocks.js'),
    };
    if (mocks[moduleName]) {
      return { filePath: mocks[moduleName], type: 'sourceFile' };
    }
  }
  return context.resolveRequest(context, moduleName, platform);
};

module.exports = config;
