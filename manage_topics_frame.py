import wx
from wx.lib.mixins.listctrl import ListCtrlAutoWidthMixin
import guiconfig
from events import TOPIC_LIST
from passage_list import (get_primary_passage_list_manager,
		lookup_passage_entry, PassageList, PassageEntry,
		InvalidPassageError, MultiplePassagesError)
from xrc.manage_topics_xrc import (xrcManageTopicsFrame,
		xrcPassageDetailsPanel, xrcTopicDetailsPanel)
from xrc.xrc_util import attach_unknown_control
from gui import guiutil
from manage_topics_operations import (ManageTopicsOperations,
		CircularDataException, BaseOperationsContext)

class ManageTopicsFrame(xrcManageTopicsFrame):
	def __init__(self, parent):
		super(ManageTopicsFrame, self).__init__(parent)
		attach_unknown_control("topic_tree", lambda parent: TopicTree(self, parent), self)
		attach_unknown_control("passage_list_ctrl", lambda parent: PassageListCtrl(self, parent), self)
		self.SetIcons(guiconfig.icons)
		self._manager = get_primary_passage_list_manager()
		self._operations_context = OperationsContext(self)
		self._operations_manager = ManageTopicsOperations(
				passage_list_manager=self._manager,
				context=self._operations_context
			)
		self._operations_manager.undo_available_changed_observers \
				+= self._undo_available_changed
		self._operations_manager.paste_available_changed_observers \
				+= self._paste_available_changed
		self._paste_available_changed()
		self._undo_available_changed()
		self._selected_topic = None
		# The topic that currently has passages displayed in the passage list
		# control.
		self._passage_list_topic = None
		self.is_passage_selected = False
		self._selected_passage = None
		self._setup_item_details_panel()
		self._init_passage_list_ctrl_headers()
		self._setup_passage_list_ctrl()
		self._setup_topic_tree()
		self._bind_events()
		self.Size = (650, 500)

	def _bind_events(self):
		self.Bind(wx.EVT_CLOSE, self._on_close)
		self.topic_tree.Bind(wx.EVT_TREE_SEL_CHANGED, self._selected_topic_changed)
		self.topic_tree.Bind(wx.EVT_TREE_ITEM_GETTOOLTIP, self._get_topic_tool_tip)
		self.topic_tree.Bind(wx.EVT_TREE_END_LABEL_EDIT, self._end_topic_label_edit)
		self.topic_tree.Bind(wx.EVT_TREE_BEGIN_LABEL_EDIT, self._begin_topic_label_edit)
		
		self.topic_tree.Bind(wx.EVT_TREE_ITEM_MENU, self._show_topic_context_menu)
		self.passage_list_ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED, self._passage_selected)
		self.passage_list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._passage_activated)
		self.passage_list_ctrl.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self._show_passage_context_menu)

		# Trap the events with the topic tree and the passage list when they
		# get focus, so that we can know which one last got focus for our
		# copy and paste operations.
		self.topic_tree.Bind(wx.EVT_SET_FOCUS, self._topic_tree_got_focus)
		self.passage_list_ctrl.Bind(wx.EVT_SET_FOCUS, self._passage_list_got_focus)

		self.passage_list_ctrl.Bind(wx.EVT_KEY_UP, self._on_char)
		self.topic_tree.Bind(wx.EVT_KEY_UP, self._on_char)

		for tool in ("cut_tool", "copy_tool", "paste_tool",
				"delete_tool", "undo_tool", "redo_tool"):
			handler = lambda event, tool=tool: self._perform_toolbar_action(event, tool)
			self.toolbar.Bind(wx.EVT_TOOL, handler, id=wx.xrc.XRCID(tool))

	def _setup_topic_tree(self):
		root = self.topic_tree.AddRoot(_("Topics"))
		self.topic_tree.SetPyData(root, self._manager)
		self._add_sub_topics(self._manager, root)
		self.topic_tree.Expand(root)

	def select_topic_and_passage(self, topic, passage_entry):
		"""Selects the given topic in the tree, and the given passage entry
		in the passage list.

		This allows the correct topic and passage to be displayed when a tag
		is clicked on.

		This assumes that the passage entry is one of the passages in the
		topic.
		"""
		self._set_selected_topic(topic)
		assert passage_entry in topic.passages
		index = topic.passages.index(passage_entry)
		self._select_list_entry_by_index(index)
		self.passage_list_ctrl.SetFocus()

	def _get_tree_selected_topic(self):
		selection = self.topic_tree.GetSelection()
		if not selection.IsOk():
			return None
		return self.topic_tree.GetPyData(selection)
	
	def _set_selected_topic(self, topic):
		tree_item = self._find_topic(self.topic_tree.GetRootItem(), topic)
		assert tree_item is not None
		self.topic_tree.SelectItem(tree_item)
		self.topic_tree.EnsureVisible(tree_item)
		return tree_item

	def _selected_topic_changed(self, event):
		# Topic nodes are selected as they are dragged past, but we shouldn't
		# change the selected topic and passage list until the dragging has
		# been finished.
		if self.topic_tree._dragging:
			event.Skip()
			return

		selected_topic = self._get_tree_selected_topic()
		if selected_topic is None:
			event.Skip()
			return

		self.selected_topic = selected_topic
		self._setup_passage_list_ctrl()

		self.Title = self._get_title()
		event.Skip()

	def get_selected_topic(self):
		return self._selected_topic

	def set_selected_topic(self, new_topic):
		self._selected_topic = new_topic
		self._change_topic_details(new_topic)

	selected_topic = property(get_selected_topic, set_selected_topic)

	def _find_topic(self, tree_item, topic):
		if self.topic_tree.GetPyData(tree_item) is topic:
			return tree_item

		id, cookie = self.topic_tree.GetFirstChild(tree_item)
		while id.IsOk():
			node = self._find_topic(id, topic)
			if node is not None:
				return node
			id, cookie = self.topic_tree.GetNextChild(tree_item, cookie)

	def _get_title(self):
		"""Gets a title for the frame, based on the currently selected topic."""
		topic = self.selected_topic
		title = _("Manage Topics")
		if topic is not self._manager:
			title = "%s - %s" % (topic.full_name, title)
		return title

	def _add_sub_topics(self, parent_list, parent_node):
		parent_list.add_subtopic_observers.add_observer(
				self._add_new_topic_node,
				(parent_node,))

		parent_list.remove_subtopic_observers.add_observer(
				self._remove_topic_node,
				(parent_node,))

		parent_list.name_changed_observers.add_observer(
				self._rename_topic_node,
				(parent_node,))

		for subtopic in parent_list.subtopics:
			self._add_topic_node(subtopic, parent_node)
	
	def _add_topic_node(self, passage_list, parent_node):
		node = self.topic_tree.AppendItem(parent_node, passage_list.name)
		self.topic_tree.SetPyData(node, passage_list)
		self._add_sub_topics(passage_list, node)
	
	def _add_new_topic_node(self, parent_node, topic):
		self._add_topic_node(topic, parent_node)

	def _remove_topic_node(self, parent_node, topic):
		topic_node = self._find_topic(parent_node, topic)
		self.topic_tree.Delete(topic_node)

	def _rename_topic_node(self, parent_node, new_name):
		self.topic_tree.SetItemText(parent_node, new_name)
	
	def _get_topic_tool_tip(self, event):
		"""Gets the description for a topic.
		
		Note that this is Windows only, but it doesn't appear that there is
		any way for us to make our own tool tips without tracking the
		underlying window's mouse movements.
		"""
		event.SetToolTip(self.topic_tree.GetPyData(event.GetItem()).description)

	def _begin_topic_label_edit(self, event):
		"""This event is used to stop us editing the root node."""
		if event.GetItem() == self.topic_tree.RootItem:
			event.Veto()
	
	def _end_topic_label_edit(self, event):
		"""This event is used to update the names of topics.
		
		Any topic node can be edited, and its name will then be set based on
		the new label text.
		"""
		if not event.IsEditCancelled():
			topic = self.topic_tree.GetPyData(event.GetItem())
			self._operations_manager.set_topic_name(topic, event.GetLabel())

	def _on_char(self, event):
		"""Handles all keyboard shortcuts."""
		guiutil.dispatch_keypress(self._get_actions(), event)

	def _get_actions(self):
		"""Returns a list of actions to be used when handling keyboard
		shortcuts.
		"""
		actions = {
			(ord("C"), wx.MOD_CMD): self._operations_manager.copy,
			(ord("X"), wx.MOD_CMD): self._operations_manager.cut,
			(ord("V"), wx.MOD_CMD): self._safe_paste,
			wx.WXK_DELETE: self._operations_manager.delete,
			(ord("Z"), wx.MOD_CMD): self._operations_manager.undo,
			(ord("Y"), wx.MOD_CMD): self._operations_manager.redo,
		}

		if not self._operations_manager.can_undo:
			del actions[(ord("Z"), wx.MOD_CMD)]
		if not self._operations_manager.can_redo:
			del actions[(ord("Y"), wx.MOD_CMD)]

		return actions

	def _perform_toolbar_action(self, event, tool_id):
		"""Performs the action requested from the toolbar."""
		event.Skip()
		actions = {
			"copy_tool":	self._operations_manager.copy,
			"cut_tool":		self._operations_manager.cut,
			"paste_tool":	self._safe_paste,
			"delete_tool":	self._operations_manager.delete,
			"undo_tool":	self._operations_manager.undo,
			"redo_tool":	self._operations_manager.redo,
		}
		actions[tool_id]()

	def _undo_available_changed(self):
		"""Enables or disables the undo and redo toolbar buttons,
		based on whether these actions are available.
		"""
		self.toolbar.EnableTool(wx.xrc.XRCID("undo_tool"),
				self._operations_manager.can_undo)
		self.toolbar.EnableTool(wx.xrc.XRCID("redo_tool"),
				self._operations_manager.can_redo)

	def _paste_available_changed(self):
		"""Enables or disables the paste toolbar button."""
		self.toolbar.EnableTool(wx.xrc.XRCID("paste_tool"),
				self._operations_manager.can_paste)

	def _show_topic_context_menu(self, event):
		"""Shows the context menu for a topic in the topic tree."""
		self.selected_topic = self.topic_tree.GetPyData(event.Item)
		menu = wx.Menu()
		
		item = menu.Append(wx.ID_ANY, _("&New Topic"))
		self.Bind(wx.EVT_MENU,
				lambda e: self._create_topic(self.selected_topic),
				id=item.Id)
		
		item = menu.Append(wx.ID_ANY, _("Add &Passage"))
		self.Bind(wx.EVT_MENU,
				lambda e: self._create_passage(self.selected_topic),
				id=item.Id)

		menu.AppendSeparator()
		
		item = menu.Append(wx.ID_ANY, _("Cu&t"))
		self.Bind(wx.EVT_MENU,
				lambda e: self._operations_manager.cut,
				id=item.Id)

		item = menu.Append(wx.ID_ANY, _("&Copy"))
		self.Bind(wx.EVT_MENU,
				lambda e: self._operations_manager.copy,
				id=item.Id)

		item = menu.Append(wx.ID_ANY, _("&Paste"))
		self.Bind(wx.EVT_MENU,
				lambda e: self._safe_paste,
				id=item.Id)

		menu.AppendSeparator()
		
		item = menu.Append(wx.ID_ANY, _("Delete &Topic"))
		self.Bind(wx.EVT_MENU,
				lambda e: self._operations_manager.delete(),
				id=item.Id)
		
		self.PopupMenu(menu)

	def _safe_paste(self, operation=None):
		"""A wrapper around the operations manager paste operation that
		catches the CircularDataException and displays an error message.
		"""
		if operation is None:
			operation = self._operations_manager.paste
		try:
			operation()
		except CircularDataException:
			wx.MessageBox(_("Cannot copy the topic to one of its children."),
					_("Copy Topic"), wx.OK | wx.ICON_ERROR, self)
	
	def _create_topic(self, topic, creation_function=None):
		new_topic = self._operations_manager.add_new_topic(creation_function)
		self._set_selected_topic(new_topic)
		self.topic_details_panel.focus()

	def save_search_results(self, search_string, search_results):
		assert search_string
		self._set_selected_topic(self._manager)
		self.topic_tree.SetFocus()
		name = u"Search: %s" % search_string
		description = u"Results from the search `%s'." % search_string

		self._create_topic(self._manager,
				lambda: PassageList.create_from_verse_list(name, search_results, description)
			)
	
	def _create_passage(self, topic):
		self._set_selected_topic(topic)
		passage = PassageEntry(None)
		self._change_passage_details(passage)
		self.passage_details_panel.begin_create_passage(topic, passage)
	
	def _on_close(self, event):
		self._remove_observers(self._manager)
		self._remove_passage_list_observers()
		self._manager.save()
		event.Skip()
	
	def _remove_observers(self, parent_topic):
		parent_topic.add_subtopic_observers.remove(self._add_new_topic_node)
		parent_topic.remove_subtopic_observers.remove(self._remove_topic_node)
		for subtopic in parent_topic.subtopics:
			self._remove_observers(subtopic)
	
	def _init_passage_list_ctrl_headers(self):
		self.passage_list_ctrl.InsertColumn(0, _("Passage"))
		self.passage_list_ctrl.InsertColumn(1, _("Comment"))

	def _setup_passage_list_ctrl(self):
		self._remove_passage_list_observers()
		self.passage_list_ctrl.DeleteAllItems()
		self._passage_list_topic = self.selected_topic
		if self._passage_list_topic is None:
			return

		self._add_passage_list_observers()
		for index, passage_entry in enumerate(self.selected_topic.passages):
			self._insert_topic_passage(passage_entry, index)

		if self.selected_topic.passages:
			self._select_list_entry_by_index(0)

	def _add_passage_list_observers(self):
		self._passage_list_topic.add_passage_observers += self._insert_topic_passage
		self._passage_list_topic.remove_passage_observers += self._remove_topic_passage

	def _remove_passage_list_observers(self):
		if self._passage_list_topic is None:
			return

		self._passage_list_topic.add_passage_observers -= self._insert_topic_passage
		self._passage_list_topic.remove_passage_observers -= self._remove_topic_passage
		for passage in self._passage_list_topic.passages:
			self._remove_passage_list_passage_observers(passage)

	def _insert_topic_passage(self, passage_entry, index=None):
		if index is None:
			index = self._passage_list_topic.passages.index(passage_entry)
		self._add_passage_list_passage_observers(passage_entry)
		self.passage_list_ctrl.InsertStringItem(index, str(passage_entry))
		self.passage_list_ctrl.SetStringItem(index, 1, passage_entry.comment)

	def _remove_topic_passage(self, passage_entry, index):
		self.passage_list_ctrl.DeleteItem(index)
		self._remove_passage_list_passage_observers(passage_entry)
		if not passage_entry.parent.passages:
			self.selected_passage = None
		else:
			if len(passage_entry.parent.passages) == index:
				index -= 1
			self._select_list_entry_by_index(index)

	def _add_passage_list_passage_observers(self, passage_entry):
		passage_entry.passage_changed_observers.add_observer(self._change_passage_passage, (passage_entry,))
		passage_entry.comment_changed_observers.add_observer(self._change_passage_comment, (passage_entry,))

	def _remove_passage_list_passage_observers(self, passage_entry):
		passage_entry.passage_changed_observers -= self._change_passage_passage
		passage_entry.comment_changed_observers -= self._change_passage_comment

	def _change_passage_passage(self, passage_entry, new_passage):
		index = self.selected_topic.passages.index(passage_entry)
		self.passage_list_ctrl.SetStringItem(index, 0, str(passage_entry))

	def _change_passage_comment(self, passage_entry, new_comment):
		index = self.selected_topic.passages.index(passage_entry)
		self.passage_list_ctrl.SetStringItem(index, 1, new_comment)

	def _passage_selected(self, event):
		passage_entry = self.selected_topic.passages[event.GetIndex()]
		self.selected_passage = passage_entry
		# Do nothing.

	def get_selected_passage(self):
		return self._selected_passage

	def set_selected_passage(self, new_passage):
		self._selected_passage = new_passage
		self._change_passage_details(new_passage)

	selected_passage = property(get_selected_passage, set_selected_passage)

	def _passage_activated(self, event):
		passage_entry = self.selected_topic.passages[event.GetIndex()]
		guiconfig.mainfrm.set_bible_ref(str(passage_entry), source=TOPIC_LIST)

	def _select_list_entry_by_index(self, index):
		"""Selects the entry in the list control with the given index."""
		state = wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED
		self.passage_list_ctrl.SetItemState(index, state, state)
	
	def _show_passage_context_menu(self, event):
		"""Shows the context menu for a passage in the passage list."""
		self.selected_passage = self.selected_topic.passages[event.GetIndex()]
		menu = wx.Menu()
		
		item = menu.Append(wx.ID_ANY, _("&Open"))
		self.Bind(wx.EVT_MENU,
				lambda e: self._passage_activated(event),
				id=item.Id)
		
		menu.AppendSeparator()
		
		item = menu.Append(wx.ID_ANY, _("Cu&t"))
		self.Bind(wx.EVT_MENU,
				lambda e: self._operations_manager.cut,
				id=item.Id)

		item = menu.Append(wx.ID_ANY, _("&Copy"))
		self.Bind(wx.EVT_MENU,
				lambda e: self._operations_manager.copy,
				id=item.Id)

		item = menu.Append(wx.ID_ANY, _("&Paste"))
		self.Bind(wx.EVT_MENU,
				lambda e: self._safe_paste,
				id=item.Id)

		menu.AppendSeparator()
		
		item = menu.Append(wx.ID_ANY, _("&Delete"))
		self.Bind(wx.EVT_MENU,
				lambda e: self._operations_manager.delete,
				id=item.Id)
		
		self.PopupMenu(menu)

	def _topic_tree_got_focus(self, event):
		self.is_passage_selected = False
		self._change_topic_details(self.selected_topic)
		event.Skip()

	def _passage_list_got_focus(self, event):
		self.is_passage_selected = True
		self._change_passage_details(self.selected_passage)
		event.Skip()

	def _setup_item_details_panel(self):
		self.topic_details_panel = TopicDetailsPanel(
				self.item_details_panel, self._operations_manager
			)
		self.topic_details_panel.Hide()
		self.item_details_panel.Sizer.Add(self.topic_details_panel, 1, wx.GROW)
		self.passage_details_panel = PassageDetailsPanel(
				self.item_details_panel, self._operations_manager
			)
		self.passage_details_panel.Hide()
		self.item_details_panel.Sizer.Add(self.passage_details_panel, 1, wx.GROW)

	def _change_topic_details(self, new_topic):
		if new_topic is None:
			return

		self.topic_details_panel.set_topic(new_topic)
		self._switch_item_details_current_panel(self.topic_details_panel)

	def _change_passage_details(self, new_passage):
		if new_passage is None:
			return

		self.passage_details_panel.set_passage(new_passage)
		self._switch_item_details_current_panel(self.passage_details_panel)

	def _switch_item_details_current_panel(self, new_panel):
		"""Makes the given panel the currently displayed item details panel."""
		# Avoid dead object errors.
		if not self:
			return

		assert new_panel in self.item_details_panel.Children
		for window in self.item_details_panel.Children:
			if window is not new_panel:
				window.Hide()
		new_panel.Show()
		self.passage_list_pane.Sizer.Layout()

# Specifies what type of dragging is currently happening with the topic tree.
# This is needed since it has to select and unselect topics when dragging and
# after dragging differently depending on whether a passage or a topic is
# being dragged.
DRAGGING_NONE = 0
DRAGGING_TOPIC = 1
DRAGGING_PASSAGE = 2

class TopicTree(wx.TreeCtrl):
	"""A tree control that handles dragging and dropping for topics.
	
	This contains code taken from the DragAndDrop tree mixin, but adapted to
	the topic tree (the DragAndDrop mixin doesn't work when you want to use
	the root node), and it selected the nodes it was being dragged past,
	meaning that the passage list kept changing.
	"""
	def __init__(self, topic_frame, *args, **kwargs):
		style = wx.TR_EDIT_LABELS | wx.TR_HAS_BUTTONS | wx.TR_LINES_AT_ROOT
		kwargs["style"] = style
		self._topic_frame = topic_frame
		super(TopicTree, self).__init__(*args, **kwargs)
		self.Bind(wx.EVT_TREE_BEGIN_DRAG, self.on_begin_drag)
		self._drag_item = None
		self._dragging = DRAGGING_NONE
		self.SetDropTarget(TopicPassageDropTarget(self))

	def on_begin_drag(self, event):
		# We allow only one item to be dragged at a time, to keep it simple
		self._drag_item = event.GetItem()
		if self._drag_item and self._drag_item != self.GetRootItem():
			self.start_dragging_topic()
			event.Allow()
		else:
			event.Veto()

	def on_end_drag(self, event):
		self.stop_dragging()
		drop_target = event.GetItem()
		if not drop_target:
			drop_target = None
		if self.is_valid_drop_target(drop_target):
			self.UnselectAll()
			if drop_target is not None:
				self.SelectItem(drop_target)
			self.on_drop_topic(drop_target, self._drag_item)

	def on_motion_event(self, event):
		if not event.Dragging():
			self.stop_dragging()
			return
		self.on_dragging(event.GetX(), event.GetY())
		event.Skip()

	def on_dragging(self, x, y):
		item, flags = self.HitTest(wx.Point(x, y))
		if not item:
			item = None
		if self.is_valid_drop_target(item):
			if self._dragging == DRAGGING_TOPIC:
				self.set_cursor_to_dragging()
		else:
			self.set_cursor_to_dropping_impossible()
		if flags & wx.TREE_HITTEST_ONITEMBUTTON:
			self.Expand(item)
		if self.GetSelections() != [item]:
			self.UnselectAll()
			if item:
				self.SelectItem(item)
		
	def start_dragging_topic(self):
		self._dragging = DRAGGING_TOPIC
		self.Bind(wx.EVT_MOTION, self.on_motion_event)
		self.Bind(wx.EVT_TREE_END_DRAG, self.on_end_drag)
		self.set_cursor_to_dragging()

	def start_dragging_passage(self, x, y):
		self._dragging = DRAGGING_PASSAGE
		self._drag_item = self.GetSelection()
		self.set_cursor_to_dragging()
		self.on_dragging(x, y)

	def stop_dragging(self):
		self._dragging = DRAGGING_NONE
		self.Unbind(wx.EVT_MOTION)
		self.Unbind(wx.EVT_TREE_END_DRAG)
		self.reset_cursor()
		self.UnselectAll()
		self.SelectItem(self._drag_item)

	def set_cursor_to_dragging(self):
		self.SetCursor(wx.StockCursor(wx.CURSOR_HAND))
		
	def set_cursor_to_dropping_impossible(self):
		self.SetCursor(wx.StockCursor(wx.CURSOR_NO_ENTRY))
		
	def reset_cursor(self):
		self.SetCursor(wx.NullCursor)

	def is_valid_drop_target(self, drop_target):
		if not drop_target: 
			return False
		elif self._dragging == DRAGGING_TOPIC:
			all_children = self._get_item_children(self._drag_item, recursively=True)
			parent = self.GetItemParent(self._drag_item) 
			return drop_target not in [self._drag_item, parent] + all_children
		else:
			return True

	def on_drop_topic(self, drop_target, drag_target):
		drag_topic = self.GetPyData(drag_target)
		drop_topic = self.GetPyData(drop_target)
		if drag_topic is drop_topic:
			return
		parent = self.GetParent()
		self._topic_frame._safe_paste(
			lambda: self._topic_frame._operations_manager.do_copy(
				drag_topic, drop_topic, keep_original=False
			)
		)

	def on_drop_passage(self, passage_entry, x, y, drag_result):
		"""Drops the given passage onto the topic with the given x and y
		coordinates in the tree.
		The drag result specifies whether the passage should be copied or
		moved.
		"""
		self.stop_dragging()

		drop_target, flags = self.HitTest(wx.Point(x, y))
		if not drop_target:
			return

		if drag_result not in (wx.DragCopy, wx.DragMove):
			return

		self.UnselectAll()
		self.SelectItem(self._drag_item)
		drop_topic = self.GetPyData(drop_target)
		keep_original = (drag_result != wx.DragMove)
		self._topic_frame._operations_manager.do_copy(
				passage_entry, drop_topic, keep_original
			)

	def _get_item_children(self, item=None, recursively=False):
		""" Return the children of item as a list. """
		if not item:
			item = self.GetRootItem()
			if not item:
				return []
		children = []
		child, cookie = self.GetFirstChild(item)
		while child:
			children.append(child)
			if recursively:
				children.extend(self._get_item_children(child, True))
			child, cookie = self.GetNextChild(item, cookie)
		return children

class PassageListCtrl(wx.ListCtrl, ListCtrlAutoWidthMixin):
	"""A list control for the passage list in the topic manager.

	This is included so that we can get the auto width mixin for the list
	control, meaning that the comment will be resized to take all the
	available space.
	"""
	def __init__(self, parent, topic_frame):
		wx.ListCtrl.__init__(self, parent,
			style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
		)
		ListCtrlAutoWidthMixin.__init__(self)
		self.Bind(wx.EVT_LIST_BEGIN_DRAG, self._start_drag)
		self._drag_index = -1
		self._topic_frame = topic_frame
		self.SetDropTarget(PassageListDropTarget(self))

	def _start_drag(self, event):
		"""Starts the drag and registers a drop source for the passage."""
		self._drag_index = event.GetIndex()
		passage_entry = self._topic_frame.selected_topic.passages[self._drag_index]
		id = passage_entry.get_id()

		data = wx.CustomDataObject("PassageEntry")
		data.SetData(str(id))
		drop_source = wx.DropSource(self)
		drop_source.SetData(data)
		result = drop_source.DoDragDrop(wx.Drag_DefaultMove)

	def _handle_drop(self, x, y, drag_result):
		"""Handles moving the passage to the new location."""
		index, flags = self.HitTest(wx.Point(x, y))
		if index == wx.NOT_FOUND or index == self._drag_index:
			return

		# XXX: This does not handle copying the passage.
		self._topic_frame._operations_manager.move_current_passage(new_index=index)

class PassageListDropTarget(wx.PyDropTarget):
	"""Allows passages to be reordered in the current topic.

	XXX: This just displays an ordinary mouse cursor.  It doesn't give any
	indication whether the passage is going to be dropped above or below the
	current topic.
	"""
	def __init__(self, list_ctrl):
		wx.PyDropTarget.__init__(self)
		self._list_ctrl = list_ctrl

		self.data = wx.CustomDataObject("PassageEntry")
		self.SetDataObject(self.data)

	def OnData(self, x, y, result):
		"""Handles a drop event by passing it back to the list control."""
		if self.GetData():
			self._list_ctrl._handle_drop(x, y, result)
		return result

class TopicPassageDropTarget(wx.PyDropTarget):
	"""This drop target allows passages to be moved to different topics in
	the topic tree.
	"""
	def __init__(self, topic_tree):
		wx.PyDropTarget.__init__(self)
		self._topic_tree = topic_tree

		self.data = wx.CustomDataObject("PassageEntry")
		self.SetDataObject(self.data)

	def OnEnter(self, x, y, result):
		self._topic_tree.start_dragging_passage(x, y)
		return result

	def OnLeave(self):
		self._topic_tree.stop_dragging()

	def OnDragOver(self, x, y, result):
		self._topic_tree.on_dragging(x, y)
		return result

	def OnData(self, x, y, result):
		if self.GetData():
			passage_id = int(self.data.GetData())
			passage_entry = lookup_passage_entry(passage_id)
			self._topic_tree.on_drop_passage(passage_entry, x, y, result)
		return result

class TopicDetailsPanel(xrcTopicDetailsPanel):
	def __init__(self, parent, operations_manager):
		super(TopicDetailsPanel, self).__init__(parent)
		self.topic = None
		self.name_text.Bind(wx.EVT_KILL_FOCUS, self._lost_focus)
		self.description_text.Bind(wx.EVT_KILL_FOCUS, self._lost_focus)
		self._operations_manager = operations_manager

	def set_topic(self, new_topic):
		"""Sets a new topic to edit with this panel."""
		if new_topic is self.topic:
			return

		self.topic = new_topic
		self.name_text.Value = new_topic.name
		self.description_text.Value = new_topic.description

	def focus(self):
		"""Sets the focus on this panel for editing."""
		self.name_text.SetFocus()
		self.name_text.SetSelection(-1, -1)

	def _lost_focus(self, event):
		if not self.topic:
			event.Skip()
			return

		name = self.name_text.Value
		description = self.description_text.Value
		self._operations_manager.set_topic_details(self.topic, name, description)

class PassageDetailsPanel(xrcPassageDetailsPanel):
	def __init__(self, parent, operations_manager):
		super(PassageDetailsPanel, self).__init__(parent)
		self.passage = None
		self.passage_text.Bind(wx.EVT_KILL_FOCUS, self._lost_focus)
		self.comment_text.Bind(wx.EVT_KILL_FOCUS, self._lost_focus)
		self._operations_manager = operations_manager
		self._creating_passage = False
		self._parent_topic = None

	def set_passage(self, new_passage):
		if new_passage is self.passage:
			return

		self.passage = new_passage
		reference = str(new_passage)
		self.passage_text.Value = reference
		self.comment_text.Value = new_passage.comment
		self.passage_preview.SetReference(reference)

	def begin_create_passage(self, parent_topic, passage):
		self._creating_passage = True
		self._parent_topic = parent_topic
		self.set_passage(passage)
		self.focus()

	def _create_passage(self):
		assert self._creating_passage
		self._creating_passage = False
		self._parent_topic = None
		if not str(self.passage):
			return

		self._operations_manager.insert_item(self.passage)

	def focus(self):
		"""Sets the focus on this panel for editing."""
		self.passage_text.SetFocus()
		self.passage_text.SetSelection(-1, -1)

	def _lost_focus(self, event):
		if not self.passage:
			event.Skip()
			return

		try:
			passage = self.passage_text.Value
			comment = self.comment_text.Value
			allow_undo = not self._creating_passage
			self._operations_manager.set_passage_details(self.passage, passage, comment, allow_undo)
			self.passage_preview.SetReference(str(self.passage))
		except InvalidPassageError:
			wx.MessageBox(_("Unrecognised passage `%s'.") % passage,
					"", wx.OK | wx.ICON_INFORMATION, self)
		except MultiplePassagesError:
			wx.MessageBox(_("Passage `%s' contains multiple passages.\n"
					"Only one verse or verse range can be entered.") % passage,
					"", wx.OK | wx.ICON_INFORMATION, self)

		if self._creating_passage and self.FindFocus() not in self.Children:
			self._create_passage()

class OperationsContext(BaseOperationsContext):
	"""Provides a context for passage list manager operations.

	This gives access to which passage and topic are currently selected in
	the manager.
	"""
	def __init__(self, frame):
		self._frame = frame

	def get_selected_topic(self):
		return self._frame.selected_topic

	def get_selected_passage(self):
		return self._frame.selected_passage

	def is_passage_selected(self):
		return self._frame.is_passage_selected

if __name__ == "__main__":
	app = wx.App(0)
	guiconfig.load_icons()
	__builtins__._ = lambda str: str
	frame = ManageTopicsFrame(None)
	frame.Show()
	app.MainLoop()
