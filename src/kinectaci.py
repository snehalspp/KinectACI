#!/usr/bin/env python
# -*- coding: utf-8 -*-


#   Copyright 2011 Peter Morton & Matthew Yeung
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#	   http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.


from PyQt4 import QtCore, QtGui
import PyQGLViewer
import OpenGL.GL as ogl
import numpy as np
import time
import fluidsynth
import freenect
from Onboard.Keyboard import Keyboard

# Play with these constants to change the system response

PLAY_TIME = 0.1			# Minimum time a note will sound for
REPEAT_TIME = 0.1		  # Minimum time between the start of two notes
SAMPLE_STRIDE = 2		  # Divide depth map resolution by this amount
MIN_POINTS = 4			 # Minimum points in a key for it to be pressed

KB_WIDTH_FAC = 1		 # Width of keyboard = Length * KB_WIDTH_FAC
KB_HEIGHT_FAC = 1	   # Height of keyboard = Length * KB_HEIGHT_FAC
KB_GAP_FAC = 0.01		  # Gap between keys = KB Length * KB_GAP_FAC

KB_NUM_KEYS = 4		   # Only dealing with white keys for now
KB_START_KEY = 0		   # 0 = C2, 1 = D2, etc... (whites only)

# Precompute U, V coordinates (since they never change)
U, V = np.meshgrid(np.arange(0,640, SAMPLE_STRIDE), 
				   np.arange(0,480, SAMPLE_STRIDE))


def get_quads(vmin, vmax):
	""" Return the 6 faces of a rectangluar prism defined by (vmin, vmax). """
	x1, y1, z1, x2, y2, z2 = np.hstack((vmin, vmax))
		
	return np.array([[x1, y1, z1], [x1, y2, z1], [x2, y2, z1], [x2, y1, z1],
					 [x1, y1, z2], [x2, y1, z2], [x2, y2, z2], [x1, y2, z2],
					 [x1, y1, z1], [x1, y1, z2], [x1, y2, z2], [x1, y2, z1],
					 [x2, y1, z1], [x2, y2, z1], [x2, y2, z2], [x2, y1, z2],
					 [x1, y1, z1], [x2, y1, z1], [x2, y1, z2], [x1, y1, z2],
					 [x1, y2, z1], [x1, y2, z2], [x2, y2, z2], [x2, y2, z1]]).T


def depth_to_xyz(u, v, stride, depth):
	""" Convert depth map to cartesian coordinates. 
	
	Parameters as originally determined by Zephod (? I think). Or found on
	the OpenKinect.org mailing list
	
	"""
	
	depth_flipped = depth[::-stride, ::stride]
	valid = depth_flipped != 2047	# Non-return = 2047
	
	us = u[valid].flatten()
	vs = v[valid].flatten()
	ds = depth_flipped[valid]

	KinectMinDistance = -10
	KinectDepthScaleFactor = .0021
	
	zz = 100.0 / (-0.00307 * ds + 3.33)
	xx = (us - 320) * (zz + KinectMinDistance) * KinectDepthScaleFactor
	yy = (vs - 240) * (zz + KinectMinDistance) * KinectDepthScaleFactor
	zz = -(zz - 200)	# Move sensor from origin (easier for displaying)
	
	points = np.vstack((xx,yy,zz)).astype(float)
	return points					 


class Key(object):
	""" Represents a key's state, position and colour. """
	def __init__(self, note, vmin, vmax, colour=(0,0,1,0.5)):
		""" Create a key corresponding to a midi note. """
		self.note = note		
		self.vmin = np.array(vmin)
		self.vmax = np.array(vmax)		
		self.colour = colour
		self.quads = get_quads(self.vmin, self.vmax)
		self.pressed = False
		self.last_pressed = 0

	def update(self, points):
		""" Update the key's press status by using the 3D points. """
		# Compute how many points are within the extents of the key
		big_enough = (points > self.vmin.reshape((3, -1))).min(axis=0)
		small_enough = (points < self.vmax.reshape((3, -1))).min(axis=0)		
		inkey_indices = np.multiply(big_enough, small_enough)		
		
		if(sum(inkey_indices) > MIN_POINTS):
			self.press()
		else:
			self.release()
	   
	def press(self):
		""" Plays the note if the key was previously unpressed. """
		press_time = time.clock()
		
		if not(self.pressed) and press_time - self.last_pressed > PLAY_TIME:
			self.pressed = True
			Key.synth.noteon(0, self.note, 127)			
			self.last_pressed = press_time
		
	def release(self):
		""" Stop the note if the key was previously pressed. """
		unpress_time = time.clock()
		
		if self.pressed and unpress_time - self.last_pressed > REPEAT_TIME:
			self.pressed = False
			Key.synth.noteoff(0, self.note)  


class Keyboard(object):
	""" Represents the virtual keyboard.
	
	Handles drawing as well as math for transformations.
	
	"""
	
	def __init__(self, number_of_keys, width_factor, height_factor, gap_factor, key_start, filename):
		""" Create the keyboard. """		
		
		self.vmin = np.array([0,0,0])
		self.vmax = np.array([1, width_factor, height_factor])
		self.scale = 1.0
		self.number_of_keys = number_of_keys
		self.width_factor = width_factor
		self.height_factor = height_factor
		self.gap_factor = gap_factor
		self.key_start = key_start
		# Load previous transform from file (if exists)
		try:
			self.set_transform(np.load(filename))
			print('transform loaded from file')
		except:
			print('failed to load from file')
			self.set_transform(np.diag([100, 100, 100, 1]))
		
		# Compute the midi note value for a few octaves
		white_basis = np.array([0, 2, 4, 5, 7, 9, 11])
		black_basis = np.array([1, 3, 6, 8, 10])
		white_notes = np.hstack((white_basis + 36,
								 white_basis + 48, 
								 white_basis + 60, 
								 white_basis + 72))  
		black_notes = np.hstack((black_basis + 36,
								 black_basis + 48, 
								 black_basis + 60, 
								 black_basis + 72))
								 
		def make_white_key(number, note):
			xmin = number * 1.0 / self.number_of_keys + self.gap_factor / 2
			xmax = (number + 1) * 1.0 / self.number_of_keys - self.gap_factor / 2
			ymin = self.vmin[1] 
			ymax = self.vmax[1]
			zmin = self.vmin[2]
			zmax = self.vmax[2]
			return Key(note, [xmin, ymin, zmin], [xmax, ymax, zmax])
			
		whites = white_notes[self.key_start:self.key_start + self.number_of_keys]
		self.keys = map(make_white_key, range(0, self.number_of_keys), whites)
		
		# Create the synthesiser - and pass it to Key class
		self.synth = fluidsynth.Synth()
		self.synth.start('alsa')
		sfid = self.synth.sfload('/usr/share/sounds/sf2/FluidR3_GM.sf2')
		self.synth.program_select(0, sfid, 0, 0) 
		Key.synth = self.synth
			
	def set_transform(self, transform):
		""" Update the internal transform, calculate inverse, and save it. """
		self.transform = transform
		self.inv_transform = np.linalg.inv(transform)
		np.save('keyboard_transform', self.transform)
	
	def nudge_roll(self, sign):
		""" Rotate about local y axis. """
		delta = np.eye(4)		
		t = sign * self.scale * 0.001
		c, s = np.cos(t), np.sin(t)
		
		Ry = np.array([[c, 0, -s], [0, 1, 0], [s, 0, c]])
		delta[0:3, 0:3] = Ry
		
		new_t = np.dot(self.transform, delta)
		self.set_transform(new_t)
	def nudge_pitch(self, sign):
		""" rotate about x axis """
		delta = np.eye(4)		
		t = sign * self.scale * 0.01
		c, s = np.cos(t), np.sin(t)
		
		Rx = np.array([[1, 0, 0], [0, c, -s], [ 0, s, c]])
		delta[0:3, 0:3] = Rx
		
		new_t = np.dot(self.transform, delta)
		self.set_transform(new_t)
	def nudge_yaw(self, sign):
		""" rotate about x axis """
		delta = np.eye(4)		
		t = sign * self.scale * 0.01
		c, s = np.cos(t), np.sin(t)
		
		Rz = np.array([[c, -s, 0], [s, c, 0], [ 0, 0, 1]])
		delta[0:3, 0:3] = Rz
		
		new_t = np.dot(self.transform, delta)
		self.set_transform(new_t)

	def nudge_x(self, sign):
		""" Move along the x axis"""
		delta = np.zeros((4,4))
		translation = self.transform[0:3, 0] * self.scale * 0.001 * sign
		delta[0:3, 3] = translation
		self.set_transform(self.transform + delta)

	def nudge_y(self, sign):
		""" Move along the y axis"""
		delta = np.zeros((4,4))
		translation = self.transform[0:3,1] * self.scale * 0.001 * sign
		delta[0:3, 3] = translation
		self.set_transform(self.transform + delta)
		
	
	def nudge_z(self, sign):
		""" Move along local z axis. """
		delta = np.zeros((4,4))
		translation = self.transform[0:3, 2] * self.scale * 0.001 * sign
		delta[0:3, 3] = translation
		self.set_transform(self.transform + delta)
		
	def update(self, points):
		""" Update state using points """
		
		# Convert points into local coordinate frame
		H = self.inv_transform
		pointsT = np.dot(H[0:3,0:3], points) + H[0:3, 3].reshape((3,1))
		
		# Clip to keyboard dimensions (speeds up later processing)
		big_enough = (pointsT > self.vmin.reshape((3, -1))).min(axis=0)
		small_enough = (pointsT < self.vmax.reshape((3, -1))).min(axis=0)		
		valid_indices = np.multiply(big_enough, small_enough)
		valid_pts = pointsT[:, valid_indices]	   
		
		# Update all the keys 
		for k in self.keys:
			k.update(valid_pts)
		
	def draw(self):
		""" Draw the keys. """
		
		ogl.glPushMatrix()
		ogl.glMultMatrixf(self.transform.T)
		
		# Draw notes
		for k in self.keys:
			if k.pressed:
				ogl.glColor4fv([0,1,0,0.4])				
			else:
				ogl.glColor4fv(k.colour)
			ogl.glVertexPointer(3, ogl.GL_FLOAT, 0, k.quads.T)
			ogl.glDrawArrays(ogl.GL_QUADS, 0, k.quads.shape[1])

		ogl.glPopMatrix()


class Viewer(PyQGLViewer.QGLViewer):
	""" Subclass PyQGLViewer to provide additional functionality. """
	
	def __init__(self):
		PyQGLViewer.QGLViewer.__init__(self)		
		self.points = np.zeros((3,1))
		
	
	def init(self):
		""" For initialisation once OpenGL context is created. """
		self.setAnimationPeriod(33)
		
		ogl.glDisable(ogl.GL_LIGHTING)
		ogl.glEnableClientState(ogl.GL_VERTEX_ARRAY)
		ogl.glEnable(ogl.GL_BLEND)
		ogl.glBlendFunc(ogl.GL_SRC_ALPHA, ogl.GL_ONE_MINUS_SRC_ALPHA)
		ogl.glEnable(ogl.GL_CULL_FACE)
		ogl.glPointSize(2.0)

		self.setStateFileName('keyboard_anywhere.xml')
		if not self.restoreStateFromFile():
			self.camera().setSceneRadius(500)
		
		# Make key commands appear in the help
		self.kbt = ['lower left', 'lower right', 'upper left']
		self.setKeyDescription(QtCore.Qt.Key_1, 
				'set the {0} point of the keyboard'.format(self.kbt[0]))		
		self.setKeyDescription(QtCore.Qt.Key_2, 
				'set the {0} point of the keyboard'.format(self.kbt[1]))		
		self.setKeyDescription(QtCore.Qt.Key_3, 
				'set the {0} point of the keyboard'.format(self.kbt[2]))
		self.setKeyDescription(QtCore.Qt.Key_Z,
				'shift the keyboard slightly in the local +Z direction')
		self.setKeyDescription(QtCore.Qt.ShiftModifier + QtCore.Qt.Key_Z,
				'shift the keyboard slightly in the local -Z direction')
		self.setKeyDescription(QtCore.Qt.Key_Plus, 
				'rotate the keyboard slightly about the local +Y axis')
		self.setKeyDescription(QtCore.Qt.Key_Minus, 
				'rotate the keyboard slightly about the local -Y axis')

		
		self.tilt = 0; 
		self.kb_corners = np.zeros((3,3))
		self.kb_corner_index = 0				
		self.keyboards = [Keyboard(4, 1, 1, 0.001, 0,"keyboard1.npy"), \
						Keyboard(4, 1, 1, 0.001, -8, "keyboard2.npy"), \
						Keyboard(1, 2, 2, 0.01, -16, "keyboard3.npy")]
		self.num_keyboard = 3
		self.keyboard = self.keyboards[0]
		
	def animate(self):
		""" Get the latest data from the kinect, and update the state. """

			
		depth, timestamp = freenect.sync_get_depth()

		xyz = depth_to_xyz(U, V, SAMPLE_STRIDE, np.array(depth))
		self.points = xyz
		for board in self.keyboards :
			board.update(self.points)
	
	def draw(self):
		""" Draw the point cloud and keyboard. """ 
		ogl.glColor4f(0.6,0.6,0.6,1)
		ogl.glVertexPointer(3, ogl.GL_FLOAT, 0, self.points.T)
		ogl.glDrawArrays(ogl.GL_POINTS, 0, self.points.shape[1])
		for board in self.keyboards :
			board.draw()
	
	def keyPressEvent(self, event):
		""" Handle keyboard events. """		
		if event.key() == QtCore.Qt.Key_F1:
			self.kb_corner_index = 0
			self.displayMessage('shift + click to set {0} corner'.format(self.kbt[0]))
		elif event.key() == QtCore.Qt.Key_F2:
			self.kb_corner_index = 1
			self.displayMessage('shift + click to set {0} corner'.format(self.kbt[1]))
		elif event.key() == QtCore.Qt.Key_F3:
			self.kb_corner_index = 2
			self.displayMessage('shift + click to set {0} corner'.format(self.kbt[2]))
		elif event.key() == QtCore.Qt.Key_Z:
			# Shift the keyboard in Z	
			if event.modifiers() and QtCore.Qt.ShiftModifier:
				self.keyboard.nudge_z(-1)
			else:
				self.keyboard.nudge_z(1)					   
			self.updateGL()
		elif event.key() == QtCore.Qt.Key_R:
			self.keyboard.nudge_pitch(1)
			self.updateGL()
		elif event.key() == QtCore.Qt.Key_F:
			self.keyboard.nudge_pitch(-1)
			self.updateGL()
		elif event.key() == QtCore.Qt.Key_E:
			self.keyboard.nudge_yaw(1)
			self.updateGL()
		elif event.key() == QtCore.Qt.Key_Q:
			self.keyboard.nudge_yaw(-1)
			self.updateGL()
		elif event.key() == QtCore.Qt.Key_A:
			self.keyboard.nudge_x(-1)
			self.updateGL()
		elif event.key() == QtCore.Qt.Key_D:
			self.keyboard.nudge_x(1)
			self.updateGL()
		elif event.key() == QtCore.Qt.Key_W:
			self.keyboard.nudge_y(1)
			self.updateGL()
		elif event.key() == QtCore.Qt.Key_S:
			self.keyboard.nudge_y(-1)
			self.updateGL()
		elif event.key() == QtCore.Qt.Key_C:
			# Rotate the keyboard
			self.keyboard.nudge_roll(1)  
			self.updateGL()
		elif event.key() == QtCore.Qt.Key_X:	  
			# Rotate the keyboard
			self.keyboard.nudge_roll(-1)
			self.updateGL()
		elif event.key() == QtCore.Qt.Key_Plus:
			self.keyboard.scale += 1
			self.displayMessage("Transform scale is set to {0}".format(self.keyboard.scale))
		elif event.key() == QtCore.Qt.Key_Minus:
			if self.keyboard.scale > 1.0 :
				self.keyboard.scale -= 1
				self.displayMessage("Transform scale is set to {0}".format(self.keyboard.scale))
		elif event.key() == QtCore.Qt.Key_1:
			self.keyboard = self.keyboards[0]
			self.displayMessage("Seleccted first keyboard")
		elif event.key() == QtCore.Qt.Key_2:
			self.keyboard = self.keyboards[1]
			self.displayMessage("Selected second keyboard")
		elif event.key() == QtCore.Qt.Key_3:
			self.keyboard = self.keyboards[2]
			self.displayMessage("Selected third keyboard")
		else:
			PyQGLViewer.QGLViewer.keyPressEvent(self, event)
	
	def compute_keyboard_transformation(self):
		""" Compute the keyboard transform from the corner points. """
		
		def unitize(v):
			return v / np.linalg.norm(v)
			
		translation = self.kb_corners[:, 0]	

		x_axis = np.subtract(self.kb_corners[:, 1], self.kb_corners[:, 0])
		scale = np.linalg.norm(x_axis)	# Length of keyboard	   
	   
		planar_vec = np.subtract(self.kb_corners[:, 2], self.kb_corners[:, 0])
		z_axis = unitize(np.cross(x_axis, planar_vec)) * scale
		y_axis = -unitize(np.cross(x_axis, z_axis)) * scale			
	   
		rot_scale = np.vstack((x_axis, y_axis, z_axis)).T
		
		# H stores the computed transform
		H = np.eye(4)
		H[0:3, 0:3] = rot_scale
		H[0:3, 3] = translation
		
		self.keyboard.set_transform(H)
	
	def select(self, event):
		""" Handler for mouse select event. """
		pos = event.pos()		
		pt, ok = self.camera().pointUnderPixel(pos)
		if(ok):
			cnr_txt = self.kbt[self.kb_corner_index]
			self.displayMessage('{0} corner is set'.format(cnr_txt))
			self.kb_corners[:, self.kb_corner_index] = list(pt)			
			self.compute_keyboard_transformation()

	def helpString(self):
		""" Text shown in help window. """
		output = "<h2>keyboard-anywhere</h2>"
		output += "<p>Press ENTER to start/stop live display of Kinect Data.</p>"
		output += "<p>Press 1, 2 or 3 to set the keyboard anchor points.</p>"
		output += "<p>Press the virtual keys to play!</p>"
		return output
	
if __name__ == '__main__':
	app = QtGui.QApplication([])
	typewritter = QtGui.QWidget()
	typewritter.resize(400,300)
	typewritter.setWindowTitle("KinetACI")
	typewritter.show()
	win = Viewer()
	win.show()
	app.exec_()

