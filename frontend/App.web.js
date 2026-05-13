import React from 'react';
import { Text, View } from 'react-native';

// Test if basic rendering works
export default function App() {
  return (
    <View style={{flex:1, justifyContent:'center', alignItems:'center', backgroundColor:'#4B0082'}}>
      <Text style={{color:'white', fontSize:24}}>Lovedogs 360</Text>
      <Text style={{color:'white', fontSize:16}}>Web version loading...</Text>
    </View>
  );
}
